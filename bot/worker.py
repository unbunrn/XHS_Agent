"""Durable worker: collect -> research -> draft -> notify -> gate -> publish.

The worker can be restarted at any time.  Publishing is never retried blindly:
an interrupted or ambiguous attempt is moved to ``publish_unknown`` for the
operator to reconcile.  All timing comes from an injected clock so tests can
run the whole loop deterministically.
"""

# =============================================================================
# 中文导读：机器人的“心脏”
# =============================================================================
# 这个 Worker 是一个可以随时重启的循环，每一轮（tick）大致做这些事：
#   1. 检查要不要安排下一次采集；
#   2. 把过期没处理的审批单标记为 expired；
#   3. 把还没发出去的通知发掉；
#   4. 从任务队列领任务并执行（采集 / 写稿 / 发布）；
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
from .db import Database, get_setting, set_setting
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
from .services.drafts import (
    DraftError,
    create_draft,
    drafts_created_today,
    pending_approval_count,
    revise_draft,
)
from .services.notifications import deliver_pending, queue_outcome, recover_inflight
from .services.publish import (
    publish_approval,
    record_blocked_approval,
    recover_interrupted_publishes,
)
from .services.research import (
    ensure_research_queue,
    ingest_source,
    next_researched_event,
    recurring_sources,
    sync_sources,
)

from .timeutil import Clock, SystemClock, local_date, parse_iso, to_iso

log = get_logger(__name__)

NEXT_COLLECT_KEY = "next_collect_at"
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
        stale = recover_stale_jobs(self.db, self.clock)
        if unknown:
            log.warning("recovered %s interrupted publish attempt(s) as publish_unknown", unknown)
        if stale:
            log.warning("requeued %s stale job lease(s)", stale)
        # A fresh process must do one collection soon, regardless of when the
        # previous process last planned its next poll.
        # 新进程启动后必须尽快采集一次。如果数据库里还没有“下次采集时间”，
        # 就设成“现在”，让第一轮 tick 立刻安排采集。
        with self.db.tx() as conn:
            if get_setting(conn, NEXT_COLLECT_KEY, None) is None:
                set_setting(conn, NEXT_COLLECT_KEY, to_iso(self.clock.now()), to_iso(self.clock.now()))
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

    def ensure_collect_scheduled(self) -> datetime | None:
        # 判断“到点采集了吗”。没到点就返回 None；到点就入队一个 collect 任务，
        # 并计算下一次采集时间。
        # 去重键用“计划时间”而不是“任务类型”：同一计划时刻只会有一个任务，
        # 即使 tick 被连续调用多次也不会堆出一串重复采集。
        # 间隔加随机抖动，避免每天固定整点去打扰来源网站。
        now = self.clock.now()
        row = self.db.fetchone("SELECT value FROM settings WHERE key=?", (NEXT_COLLECT_KEY,))
        raw = None if row is None else str(row["value"])
        due = parse_iso(raw) if isinstance(raw, str) else None
        if due is None:
            due = now
        if now < due:
            return None
        key = f"collect:{to_iso(due)}"
        enqueue_job(
            self.db,
            "collect",
            key,
            run_at=now,
            clock=self.clock,
            priority=1,
            max_attempts=4,
        )
        interval = max(60, int(self.config.get("limits.poll_interval_seconds", 1800)))
        jitter = max(0, int(self.config.get("limits.poll_jitter_seconds", 120)))
        delay = interval + (self.rng.randint(0, jitter) if jitter else 0)
        with self.db.tx() as conn:
            set_setting(conn, NEXT_COLLECT_KEY, to_iso(now + timedelta(seconds=delay)), to_iso(now))
        return now + timedelta(seconds=delay)

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
    def _handle_collect(self, job) -> dict[str, Any]:
        # 采集任务：逐个抓取已启用的来源，然后确保研究队列有内容，
        # 最后看看要不要安排一篇稿子。
        # 注意：单个来源失败不会让整个任务失败——ingest_source 内部会
        # 记录该来源的错误并继续，其他来源照常抓。
        sync_sources(self.db, self.config)
        results = []
        # Commissioned topic rows are fetched by their topic job, not by the
        # recurring collector.  They deliberately have no query in the sources
        # table, so including them here would abort every collection round.
        for source in recurring_sources(self.db):
            results.append(
                ingest_source(
                    self.db,
                    self.config,
                    self.http,
                    self.clock,
                    source,
                    logger=log,
                    budget=self.budget,
                )
            )
        ensure_research_queue(self.db, self.config, self.clock)
        draft_note = self.maybe_enqueue_draft()
        return {
            "sources": len(results),
            "new_items": sum(int(r.get("new_items", 0)) for r in results),
            "draft": draft_note,
        }

    def maybe_enqueue_draft(self) -> str:
        # 三道限额，任何一道满了就不排新稿：
        #   1. 今天已经创建的草稿数；
        #   2. 当前待审批的草稿数（避免一次堆太多让用户烦）；
        #   3. 是否有已经研究完、等着写的事件。
        # 返回一句人类可读的说明，会写进任务结果的 draft 字段。
        if drafts_created_today(self.db, self.config, self.clock, manual=False) >= int(
            self.config.get("limits.max_drafts_per_day", 1)
        ):
            return "daily draft limit reached"
        if pending_approval_count(self.db) >= int(self.config.get("limits.max_pending_approvals", 3)):
            return "pending approval limit reached"
        event = next_researched_event(self.db)
        if event is None:
            return "no researched topic waiting"
        key = f"draft:{local_date(self.clock.now(), self.config.timezone_name)}"
        enqueue_job(
            self.db,
            "draft",
            key,
            payload={"event_id": int(event["id"])},
            clock=self.clock,
            priority=3,
            max_attempts=2,
        )
        return f"queued event #{event['id']}"

    def _handle_draft(self, job) -> dict[str, Any]:
        payload = job_payload(job)
        try:
            result = create_draft(
                self.db,
                self.config,
                self.clock,
                self.llm,
                event_id=payload.get("event_id"),
            )
        except DraftError as exc:
            # Limits, evidence and validation problems are policy outcomes, not
            # transient failures; retrying would manufacture a draft.
            # 这些是“规则不允许”，不是“网络抽风”。重试只会重复打印同样的
            # 错误，甚至强行造出一篇不合格的稿子，所以直接跳过。
            return {"skipped": str(exc)}
        try:
            delivered = deliver_pending(self.db, self.config, self.clock, self.notifier)
        except NotConfigured as exc:
            delivered = [{"ok": False, "error": str(exc)}]
        return {
            "draft_id": result["draft"]["id"],
            "approval_code": result["approval_code"],
            "notifications": delivered,
        }

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
            "revise": self._handle_revise,
            "publish": self._handle_publish,
        }

    # -- one tick -------------------------------------------------------
    def tick(self, *, limit: int = 20) -> dict[str, Any]:
        # 一轮完整循环。即使某一轮没有任务，也会正常返回一个汇总字典。
        # 顺序：先做“到期的杂务”（采集排期、审批过期、通知投递），
        # 再跑任务队列，最后补记指标。
        self.db.init()
        self.ensure_creator_discovery_scheduled()
        expired = expire_approvals(self.db, self.clock)
        try:
            delivered = deliver_pending(self.db, self.config, self.clock, self.notifier)
        except NotConfigured as exc:
            delivered = [{"ok": False, "error": str(exc)}]
        jobs = run_due_jobs(
            self.db,
            self.clock,
            self.handlers(),
            worker=f"worker-{self.clock.now().timestamp():.0f}",
            limit=limit,
        )
        metrics = capture_unavailable_metrics(self.db, self.clock)
        return {
            "expired": expired,
            "notifications": delivered,
            "jobs": jobs,
            "metrics_recorded_missing": metrics,
        }

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
        try:
            while not stop["flag"]:
                summary = self.tick()
                log.info("tick: %s", summary)
                ticks += 1
                if stop_after is not None and ticks >= stop_after:
                    break
                # Wake regularly so approvals and jobs become due on time.
                deadline = time.monotonic() + 30
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


def run_once(config: Config, *, clock: Clock | None = None) -> dict[str, Any]:
    # CLI / 测试用：启动一次，跑一轮 tick 就结束，不常驻。
    worker = Worker(config, clock=clock)
    worker.startup()
    return worker.tick()


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
