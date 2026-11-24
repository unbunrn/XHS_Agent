"""Durable worker: discover -> create/revise -> approve -> publish -> reconcile.

The worker can be restarted at any time.  Publishing is never retried blindly:
an interrupted or ambiguous attempt is moved to ``publish_unknown`` for the
operator to reconcile.  All timing comes from an injected clock so tests can
run the whole loop deterministically.
"""

# =============================================================================
# 中文导读：机器人的“心脏”
# =============================================================================
# 这个 Worker 是一个可以随时重启的循环，每一轮（tick）大致做这些事：
#   1. 检查要不要安排下一次选题发现；
#   2. 把过期没处理的审批单标记为 expired；
#   3. 把还没发出去的通知发掉；
#   4. 从任务队列领任务并执行（选题发现 / 创作 / 发布 / 修改）；
#   5. 记录还拿不到的数据指标（先记 missing，不编假数字）。
#
# 为什么发布“绝不盲目重试”？
#   如果请求已经发到平台、但没收到回执，重试可能让同一篇帖子发两次。
#   所以这种“不确定”的情况会被标成 publish_unknown，等人工去核对。
#   宁可少发一条，也不能重复发。
#
# 为什么所有时间都从 clock 来？
#   生产环境用真实系统时钟；测试可以注入“冻结时钟”，
#   让 30 分钟的等待变成瞬间完成，测试才能稳定又快速。
# =============================================================================

from __future__ import annotations

import random
import signal
import time
from datetime import datetime, timedelta
from typing import Any

from .adapters.llm import build_llm
from .adapters.notify import build_notifier
from .adapters.publishers import build_publisher
from .adapters.types import NotConfigured
from .audit import audit
from .budget import BudgetGuard
from .config import Config, load_config
from .db import Database, set_setting
from .editorial import RevisionUnsatisfied, change_summary_lines
from .httpclient import HttpClient
from .jobs import (
    enqueue_job,
    job_payload,
    recover_stale_jobs,
    run_due_jobs,
)
from .logging_setup import get_logger, setup_logging
from .security import SSRFGuard
from .services.analytics import capture_unavailable_metrics
from .services.approvals import expire_approvals
from .services.creator_agent.discovery import discover_aihot
from .services.creator_agent.entry import scheduled_tick
from .services.drafts import revise_draft
from .services.notifications import deliver_pending, queue_outcome, recover_inflight
from .services.publish import (
    publish_approval,
    record_blocked_approval,
    recover_interrupted_publishes,
)
from .services.research import sync_sources

from .timeutil import Clock, SystemClock, parse_iso, to_iso

log = get_logger(__name__)

NEXT_CREATOR_DISCOVERY_KEY = "next_creator_discovery_at"


class WorkerError(RuntimeError):
    # 启动阶段的致命错误，例如配置里同时开了真实发布和演示模式。
    # 这类错误必须立刻停，不能“带病运行”。
    pass


def build_http(config: Config) -> HttpClient:
    # 采集专用 HTTP 客户端：guard 不允许任何私有地址。
    # 注意下面注释：本机小红书桥接会自己建一个范围极窄的客户端，
    # 绝不复用这个采集客户端，避免把“允许本机”的能力泄漏给新闻抓取。
    network = config.section("network")
    # News collection must never reach loopback/private addresses.  The local
    # Xiaohongshu bridge builds its own narrowly-scoped client and guard; it is
    # not shared with the collector.
    guard = SSRFGuard()
    return HttpClient(
        guard=guard,
        user_agent=str(network.get("user_agent", "BotOperator/0.1")),
        timeout=float(network.get("timeout_seconds", 20)),
        max_bytes=int(network.get("max_fetch_bytes", 2_097_152)),
        min_host_interval=float(network.get("min_host_interval_seconds", 2.0)),
        max_retries=int(network.get("max_retries", 2)),
        backoff_base=float(network.get("backoff_base_seconds", 1.0)),
        backoff_max=float(network.get("backoff_max_seconds", 20.0)),
    )


class Worker:
    def __init__(
        self,
        config: Config,
        *,
        clock: Clock | None = None,
        db: Database | None = None,
        http: HttpClient | None = None,
        rng: random.Random | None = None,
    ):
        # 依赖注入：时钟、数据库、HTTP 客户端、随机数都能从外部替换。
        # 生产用默认值；测试传入临时数据库和冻结时钟。
        self.config = config
        self.clock = clock or SystemClock()
        self.db = db or Database(config.db_path)
        self.http = http or build_http(config)
        self.rng = rng or random.Random()
        self.budget = BudgetGuard(self.db, config, self.clock)
        self.llm = build_llm(config, self.clock, self.budget)
        self.notifier = build_notifier(config, self.clock)
        self.publisher = None
        try:
            self.publisher = build_publisher(config, self.clock)
        except NotConfigured as exc:
            # Read-only/demo runs may legitimately have no publisher.  The gate
            # will report blocked instead of pretending a post exists.
            # 只读/演示模式可以没有发布器；这时发布任务会明确报告 blocked，
            # 而不是假装发成功了。
            self.publisher_error = str(exc)
            log.warning("publisher is not configured: %s", exc)
        else:
            self.publisher_error = None

    # -- lifecycle ------------------------------------------------------
    def startup(self) -> dict[str, Any]:
        # 启动自检与恢复。顺序有讲究：
        #   - runtime_guards 先检查配置是否自相矛盾，不合格直接拒绝启动；
        #   - 初始化数据库、同步来源配置；
        #   - 恢复“发送中”的通知（可能上次进程崩在发送途中）；
        #   - 恢复“发布中”的尝试 -> 一律标为 publish_unknown；
        #   - 回收过期租约的任务。
        problems = self.config.runtime_guards()
        if problems:
            raise WorkerError("; ".join(problems))
        self.db.init()
        sync_sources(self.db, self.config)
        recover_inflight(self.db)
        unknown = recover_interrupted_publishes(self.db, self.clock, self.config)
        from .services.publication_agent.entry import recover_unknown_reconciliations

        reconcile = recover_unknown_reconciliations(self.db, self.clock)
        stale = recover_stale_jobs(self.db, self.clock)
        if unknown:
            log.warning("recovered %s interrupted publish attempt(s) as publish_unknown", unknown)
        if reconcile:
            log.warning("queued %s missing unknown-result reconciliation job(s)", reconcile)
        if stale:
            log.warning("requeued %s stale job lease(s)", stale)
        audit(
            self.db,
            "worker",
            "worker.started",
            entity="worker",
            entity_id=None,
            detail={"mode": self.config.mode, "demo": self.config.demo_pipeline},
            at=to_iso(self.clock.now()),
        )
        return {
            "mode": self.config.mode,
            "demo": self.config.demo_pipeline,
            "publisher": self.config.get("publisher.provider"),
            "publisher_error": self.publisher_error,
        }

    def ensure_creator_discovery_scheduled(self) -> datetime | None:
        """Queue one durable V2 discovery job per configured interval.

        The job only creates scheduled agent runs; it never approves or
        publishes.  The next-run marker is advanced at enqueue time so a
        failing job cannot create a tight retry loop.
        """
        if not bool(self.config.get("creator_agent.daily_discovery_enabled", False)):
            return None
        now = self.clock.now()
        row = self.db.fetchone("SELECT value FROM settings WHERE key=?", (NEXT_CREATOR_DISCOVERY_KEY,))
        raw = None if row is None else str(row["value"])
        due = parse_iso(raw) if isinstance(raw, str) else None
        if due is None:
            due = now
        if now < due:
            return None
        interval = max(300, int(self.config.get("creator_agent.daily_discovery_seconds", 86400)))
        key = f"creator-discovery:{to_iso(due)}"
        enqueue_job(
            self.db,
            "creator_discovery",
            key,
            run_at=now,
            clock=self.clock,
            priority=2,
            max_attempts=2,
        )
        next_at = now + timedelta(seconds=interval)
        with self.db.tx() as conn:
            set_setting(conn, NEXT_CREATOR_DISCOVERY_KEY, to_iso(next_at), to_iso(now))
        return next_at

    def _handle_creator_discovery(self, job) -> dict[str, Any]:
        # AIHot is only a lead source.  The brief carries the original URL;
        # the V2 agent still performs research and creates a pending approval.
        items = discover_aihot(
            self.http,
            self.config,
            self.clock,
            limit=max(1, int(self.config.get("creator_agent.daily_discovery_max_topics", 2))),
            max_age_hours=max(1, int(self.config.get("creator_agent.daily_discovery_max_age_hours", 48))),
            feed_limit=max(1, int(self.config.get("creator_agent.daily_discovery_feed_limit", 50))),
        )
        leads = [
            {
                "topic": item.topic,
                "source_url": item.source_url,
                "summary": item.summary,
            }
            for item in items
        ]
        try:
            account_id = str(self.config.account().get("id", "xhs-main"))
        except Exception:
            account_id = "xhs-main"
        run_ids = scheduled_tick(
            self.db,
            self.clock,
            account_id=account_id,
            discover=lambda: leads,
            max_topics=max(1, int(self.config.get("creator_agent.daily_discovery_max_topics", 2))),
        )
        return {"discovered": len(leads), "runs": run_ids}

    # -- job handlers ---------------------------------------------------
    def _handle_creator_agent(self, job) -> dict[str, Any]:
        # V2: the model-driven loop runs here, never in the Feishu callback.
        # The runtime creates a real pending approval only at submit time.
        from .services.creator_agent.context import AgentContext
        from .services.creator_agent.entry import run_job
        from .services.creator_agent.live import LiveRuntime
        from .services.creator_agent.registry import ToolRegistry
        from .services.creator_agent.runner import AgentRunner
        from .services.creator_agent.store import AgentStore
        from .services.creator_agent.tools import register_tools

        if not bool(self.config.get("creator_agent.enabled", False)):
            return {"skipped": "creator_agent is disabled"}

        def factory(brief):
            store = AgentStore(self.db, self.clock)
            runtime = LiveRuntime(
                db=self.db, config=self.config, clock=self.clock,
                http=self.http, budget=self.budget, llm=self.llm,
                store=store, run_key=brief.run_key,
            )
            base = None
            if brief.parent_run_key and brief.base_artifact_version:
                base = store.artifact(
                    str(brief.parent_run_key), int(brief.base_artifact_version)
                )
            parent_materials = runtime.load_parent_artifact(base)
            seeded = None
            if brief.source_url:
                # A scheduled lead carries the original article URL.  Fetching
                # it once here is still only a seed; the model decides whether
                # to use it, search more, or ask the owner.
                try:
                    seeded = runtime.read_source(url=brief.source_url)
                except Exception as exc:
                    log.warning("scheduled source seed failed: %s", exc)
            context = AgentContext(
                brief=brief, runtime=runtime, store=store, base_artifact=base
            )
            for material in parent_materials:
                context.add_material(material)
            if seeded is not None:
                context.add_material(seeded)
            for material in store.materials(brief.run_key):
                context.add_material(material)
            registry = ToolRegistry()
            register_tools(registry, runtime)
            return AgentRunner(
                db=self.db, config=self.config, clock=self.clock,
                llm=self.llm, registry=registry, context=context,
            )

        result = run_job(self.db, self.config, self.clock, job, runner_factory=factory)
        return {
            "run_key": result.run_key,
            "status": result.status,
            "steps": result.steps,
            "artifact_version": result.artifact.version if result.artifact else None,
            "error": result.error[:200] if result.error else "",
        }

    def _handle_publication_agent(self, job) -> dict[str, Any]:
        from .services.publication_agent.live import LivePublicationRuntime
        from .services.publication_agent.runner import PublicationRunner

        if not bool(self.config.get("publication_agent.enabled", False)):
            return {"skipped": "publication_agent is disabled"}
        payload = job_payload(job)
        task_id = payload.get("task_id")
        if task_id is None:
            return {"skipped": "publication job has no task_id"}
        runtime = LivePublicationRuntime(
            self.db, self.config, self.clock, publisher=self.publisher
        )
        runner = PublicationRunner(
            self.db, self.config, self.clock, self.llm, runtime
        )
        try:
            result = runner.run(
                int(task_id),
                diagnose_only=bool(payload.get("diagnose_only")),
                reconcile_only=bool(payload.get("reconcile_only")),
            )
        except Exception as exc:  # noqa: BLE001 - a crashed agent must not look done
            return {
                "task_id": int(task_id),
                "retry": True,
                "error": f"{type(exc).__name__}: {exc}",
                "retry_after_seconds": 60,
            }
        try:
            delivered = deliver_pending(self.db, self.config, self.clock, self.notifier)
        except NotConfigured as exc:
            delivered = [{"ok": False, "error": str(exc)}]
        return {
            "task_id": result.task_id,
            "state": result.state,
            "steps": result.steps,
            "error": result.error or "",
            "notifications": delivered,
        }

    def _handle_revise(self, job) -> dict[str, Any]:
        # The Feishu/CLI callback only queued this job.  The model call happens
        # here, away from the 3-second long-connection callback.
        payload = job_payload(job)
        approval_id = payload.get("approval_id")
        instruction = str(payload.get("instruction") or "").strip()
        actor = str(payload.get("actor") or "owner")
        if approval_id is None or not instruction:
            return {"skipped": "revision job is missing approval_id/instruction"}
        outcome_key = f"revision-outcome:{int(job['id'])}"
        try:
            result = revise_draft(
                self.db,
                self.config,
                self.clock,
                self.llm,
                approval_id=int(approval_id),
                instruction=instruction,
                actor=actor,
            )
        except RevisionUnsatisfied as exc:
            # The generation ran but did not meet the owner's request.  Say so
            # explicitly; the old approval stays frozen and no new version is
            # presented as if it were a successful revision.
            lines = [
                "修改未满足：未生成可审批的新版本。",
                *change_summary_lines(exc.report),
                "原因：" + str(exc)[:500],
                "原审批仍处于修改中，未恢复可发布状态。",
            ]
            detail = "\n".join(lines)
            queue_outcome(
                self.db,
                self.config,
                self.clock,
                detail,
                dedupe_key=outcome_key,
            )
            self._deliver_notifications()
            return {"revision_unsatisfied": detail}
        except Exception as exc:  # noqa: BLE001 - report and do not auto-retry a side effect
            detail = f"修改失败：{type(exc).__name__}: {exc}"
            queue_outcome(
                self.db,
                self.config,
                self.clock,
                detail,
                dedupe_key=outcome_key,
            )
            self._deliver_notifications()
            return {"revision_failed": detail}
        changes = result.get("change_lines") or ["已生成新版本"]
        queue_outcome(
            self.db,
            self.config,
            self.clock,
            "\n".join(
                [
                    f"修改完成：新草稿 v{result['draft']['version']}，"
                    f"新审批码 {result['approval']['code']}，请重新批准。",
                    "实际修改：",
                    *[f"- {line}" for line in changes],
                ]
            ),
            dedupe_key=outcome_key,
        )
        delivered = self._deliver_notifications()
        return {
            "draft_id": result["draft"]["id"],
            "approval_code": result["approval"]["code"],
            "notifications": delivered,
        }

    def _deliver_notifications(self) -> list[dict[str, Any]]:
        try:
            return deliver_pending(self.db, self.config, self.clock, self.notifier)
        except NotConfigured as exc:
            return [{"ok": False, "error": str(exc)}]

    def _handle_publish(self, job) -> dict[str, Any]:
        # 发布任务只带着 approval_id。真正的“能不能发”全部由
        # services.publish.publish_approval 里的闸门判断：
        # 审批是否 approved、是否过期、内容指纹是否匹配、四道开关是否打开。
        # worker 自己不做任何放行判断，避免规则散落在多处。
        payload = job_payload(job)
        approval_id = payload.get("approval_id")
        if approval_id is None:
            return {"skipped": "publish job has no approval_id"}
        if self.publisher is None:
            result = record_blocked_approval(
                self.db,
                self.config,
                self.clock,
                int(approval_id),
                reason=self.publisher_error or "publisher is not configured",
                reason_code="publisher_not_configured",
            )
            try:
                result["notifications"] = deliver_pending(
                    self.db, self.config, self.clock, self.notifier
                )
            except NotConfigured as exc:
                result["notifications"] = [{"ok": False, "error": str(exc)}]
            return result
        result = publish_approval(
            self.db,
            self.config,
            self.clock,
            int(approval_id),
            publisher=self.publisher,
        )
        # Result persistence is synchronous with the publish job.  Deliver the
        # new durable outcome immediately as well; if delivery fails, the
        # notification remains queued for the next tick rather than being lost.
        try:
            result["notifications"] = deliver_pending(
                self.db, self.config, self.clock, self.notifier
            )
        except NotConfigured as exc:
            result["notifications"] = [{"ok": False, "error": str(exc)}]
        return result

    def handlers(self) -> dict[str, Any]:
        return {
            "creator_discovery": self._handle_creator_discovery,
            "creator_agent_run": self._handle_creator_agent,
            "publication_agent": self._handle_publication_agent,
            "revise": self._handle_revise,
            "publish": self._handle_publish,
        }

    # -- one tick -------------------------------------------------------
    def tick(self, *, limit: int = 20) -> dict[str, Any]:
        # =====================================================================
        # tick = “脉搏跳一次”
        # =====================================================================
        # worker 常驻进程大约每 30 秒调用一次 tick()。
        # 每一轮都从“数据库 + 当前时间”重新出发，把到期的事情做一遍，
        # 做完返回一张“成绩单”（字典），然后去休息，等下一轮。
        #
        # 这种“每轮都重新读数据库”的写法叫幂等设计：
        # 进程什么时候被杀掉都没关系，重启后的下一轮照样接着干，
        # 不需要把进度小心翼翼地保存在内存里。
        #
        # 参数说明（零基础版）：
        #   *            表示后面的参数只能“写名字”传：tick(limit=5)。
        #                不许写 tick(5)，这样读代码的人不会搞混 5 代表什么。
        #   limit=20     默认值：这一轮最多执行 20 条任务。
        #   -> dict[str, Any]
        #                类型提示：返回一个字典，键是字符串，值是任意类型。
        #                它只是“说明书”，Python 运行时并不会强制检查。
        #
        # 本轮顺序（顺序有讲究，不能乱）：
        #   1. 确认数据库结构是最新的
        #   2. 看看“每日选题发现”到点没有，到点就排进任务队列
        #   3. 把过期没人处理的审批单关掉
        #   4. 把队列里还没发出去的通知发掉
        #   5. 从任务队列领任务并执行（选题发现 / 创作 / 发布 / 修改……）
        #   6. 给已发布的帖子补记“指标暂缺”
        # =====================================================================

        self.db.init()
        # 第 1 步：数据库“开机自检”。
        # 第一次运行会建目录、建表；以后每次运行会对比 schema.py 里的版本号，
        # 只执行还没跑过的升级脚本（所以旧数据库能自动升级）。
        # 重复调用是安全的（幂等），所以放在每轮开头最省心。

        self.ensure_creator_discovery_scheduled()
        # 第 2 步：安排“每日选题发现”。
        # 内部逻辑：
        #   - 功能没开或还没到点 -> 返回 None，什么都不做；
        #   - 到点了 -> 往 jobs 表插入一条 creator_discovery 任务，
        #     并把“下次时间”往后推一个间隔。
        # 注意：这里只是“排队”，不是立刻执行；真正执行在第 5 步。
        # 好处是任务先落盘——进程就算马上崩溃，重启后任务还在队列里。

        expired = expire_approvals(self.db, self.clock)
        # 第 3 步：关掉过期审批单。
        # 一张审批单有有效期，过期后：
        #   - 还没批的 -> 作废，避免拿旧稿去发；
        #   - 已经批了、但错过计划发布时间的 -> 也作废，
        #     因为你批准的是“那个时间点的那篇内容”，不能拿旧批准去发。
        # 返回值 expired：这一轮作废了几张单子（整数）。

        try:
            delivered = deliver_pending(self.db, self.config, self.clock, self.notifier)
        except NotConfigured as exc:
            # 第 4 步：发送积压的通知（比如飞书审批消息）。
            # deliver_pending 逐条处理队列里的通知：发成功记 sent，
            # 发失败记 retry/failed，留到下一轮再试。
            #
            # 为什么包 try/except？
            # 演示模式或没配置飞书凭据时，发送会抛 NotConfigured。
            # 这不是程序坏了，只是“今天没有邮差”。
            # 捕获它以后本轮继续往下跑，错误写进成绩单方便排查。
            delivered = [{"ok": False, "error": str(exc)}]
            # delivered 是一个列表，每项记录一条通知的发送结果。

        jobs = run_due_jobs(
            self.db,           # 数据库：任务队列就存在这里
            self.clock,        # 时钟：判断任务“到点没有”；测试时可冻结时间
            self.handlers(),   # “任务类型 -> 处理函数”的对照表
            #   队列里的任务只存一个 kind 字符串（例如 "revise"），
            #   run_due_jobs 靠这张表找到该调用哪个函数。
            #   handlers() 返回的对应关系：
            #     creator_discovery  -> _handle_creator_discovery
            #     creator_agent_run  -> _handle_creator_agent
            #     publication_agent  -> _handle_publication_agent
            #     revise             -> _handle_revise
            #     publish            -> _handle_publish
            worker=f"worker-{self.clock.now().timestamp():.0f}",
            # 领任务时的“工号”，例如 worker-1760000000。
            # timestamp() 是秒级时间戳；:.0f 表示“不要小数点”。
            # 用途：任务是“租”出去的，工号用来区分是谁领走的；
            # 进程挂掉以后，别的进程能认出过期租约并把任务回收。
            limit=limit,
            # 本轮最多执行多少条任务；默认 20 条。
        )
        # 第 5 步：真正干活。
        # run_due_jobs 内部循环：领一条任务 -> 调用对应函数 -> 记录结果。
        # 关键设计：每条任务的异常被单独捕获，
        # 一条任务失败不会拖垮整轮，也不影响后面的任务。
        # jobs 是结果列表，例如：
        #   [{"job_id": 3, "kind": "revise", "state": "done"}]

        metrics = capture_unavailable_metrics(self.db, self.clock)
        # 第 6 步（收尾）：给“已发布”的帖子补一条“指标暂缺”快照。
        # 为什么要先写一条“缺”？
        # 因为“我们没去采集”和“采集了但平台没给数据”必须能区分开。
        # 以后真正的采集器可以用 record_metrics 覆盖这一行。
        # 返回值 metrics：本轮补记了几条记录（整数）。

        return {
            "expired": expired,                    # 本轮作废的审批单数量
            "notifications": delivered,            # 通知发送结果列表
            "jobs": jobs,                          # 任务执行结果列表
            "metrics_recorded_missing": metrics,   # 本轮补记的缺失指标条数
        }
        # 返回“成绩单”而不是直接打印，好处有两个：
        #   1. run_forever 里统一写日志：log.info("tick: %s", summary)；
        #   2. 测试可以直接检查字典里的数字，不用去解析日志文本。

    def run_forever(self, *, stop_after: int | None = None) -> None:
        # 常驻模式。用 signal 捕获 Ctrl+C / 终止信号，让循环优雅退出：
        # 不是立刻杀进程，而是把 stop 标志置位，等当前 tick 收尾。
        # 每轮 tick 后最多睡 30 秒，但每 0.5 秒检查一次停止标志，
        # 保证按 Ctrl+C 能很快响应。
        self.startup()
        stop = {"flag": False}

        def _stop(signum, frame):  # noqa: ARG001
            stop["flag"] = True

        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass
        log.info("worker started; mode=%s demo=%s", self.config.mode, self.config.demo_pipeline)
        ticks = 0
        last_log_at = 0.0
        try:
            while not stop["flag"]:
                summary = self.tick()
                if summary.get("jobs"):
                    # Flush any notifications queued by the jobs that just finished
                    # right away instead of waiting for the next sleep cycle.
                    try:
                        extra_delivered = deliver_pending(
                            self.db, self.config, self.clock, self.notifier
                        )
                        if extra_delivered:
                            summary["notifications"] = list(summary.get("notifications") or []) + list(extra_delivered)
                    except NotConfigured:
                        pass
                now_mono = time.monotonic()
                has_activity = bool(
                    summary.get("expired")
                    or summary.get("notifications")
                    or summary.get("jobs")
                    or summary.get("metrics_recorded_missing")
                )
                if has_activity or (now_mono - last_log_at >= 30.0) or stop_after is not None:
                    log.info("tick: %s", summary)
                    last_log_at = now_mono
                ticks += 1
                if stop_after is not None and ticks >= stop_after:
                    break
                # Wake every 2s so approvals, notifications and jobs respond quickly.
                deadline = time.monotonic() + 2.0
                while not stop["flag"] and time.monotonic() < deadline:
                    time.sleep(0.5)
        finally:
            # 无论正常结束还是异常退出，都把原来的信号处理器还回去。
            # 这是好公民行为：不劫持进程退出后其他代码的信号处理。
            for sig, handler in previous.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
            log.info("worker stopped")


def main(config_path: str | None = None) -> int:
    # 进程入口：加载配置 -> 设置日志轮转 -> 创建 Worker -> 常驻运行。
    # 返回 0 表示正常退出。
    config = load_config(config_path)
    setup_logging(
        config.log_dir,
        level=str(config.get("app.log_level", "INFO")),
        max_bytes=int(config.get("app.log_max_bytes", 5_000_000)),
        backups=int(config.get("app.log_backups", 5)),
    )
    worker = Worker(config)
    worker.run_forever()
    return 0
