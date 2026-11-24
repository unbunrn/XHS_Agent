"""Small durable job queue on SQLite.

Jobs are leased transactionally.  A crashed worker leaves ``state='running'``
until its lease expires; recovery re-queues it only within ``max_attempts``.
Publishing is additionally protected by the unique publish-attempt fingerprint,
so re-running a publish job can never send the post twice.
"""

# =============================================================================
# 中文导读：一个“断电也不丢任务”的待办清单
# =============================================================================
# 为什么不用内存里的队列（比如 Python 的 queue）？
#   程序一重启，内存队列就没了。这个机器人要采集、写稿、发布，
#   每个动作都可能花几十秒，中途崩溃很常见。任务必须存进 SQLite。
#
# 几个关键词：
#   pending   等待处理
#   running   已被某个 worker“领走”，带租约（lease）
#   done      成功完成
#   failed    重试次数用完，彻底失败
#   租约 lease：worker 领任务时约定“我最多占用 5 分钟”。如果它崩溃了，
#     租约到期后任务会被回收，重新排队。就像图书馆借书有到期日，
#     借书人失联了，书会自动回到可借状态。
#   dedupe_key：任务的唯一标识。同一个“发布审批 X”的任务只会存在一条，
#     重复入队会直接返回已有任务，不会重复执行。
#
# 生活化类比：
#   这是一块挂在墙上的任务板。每个人（worker）从板上取一张便签，
#   写上自己的名字和归还时间；做完就撕掉，做不完就放回去并注明原因。
#   老板（run_due_jobs）每隔一会儿来看看有没有到期的便签。
# =============================================================================

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from .audit import audit
from .db import Database, json_dumps, json_loads
from .timeutil import Clock, parse_iso, to_iso

JobHandler = Callable[[sqlite3.Row], dict[str, Any] | None]


def enqueue_job(
    db: Database,
    kind: str,
    dedupe_key: str,
    *,
    payload: dict | None = None,
    run_at: datetime | None = None,
    priority: int = 0,
    max_attempts: int = 5,
    clock: Clock,
    conn: sqlite3.Connection | None = None,
) -> int:
    # 入队。dedupe_key 保证幂等：同一个 key 已存在就直接返回旧 id，
    # 不会创建第二条任务。这是防重复发布的第一道门。
    # run_at 可以是未来时间，用来安排“稍后再试”或“到点再发”。
    # conn 允许调用方把“状态冻结 + 入队”放进同一个事务。
    now = clock.now()
    at = to_iso(run_at or now)

    def _insert(target: sqlite3.Connection) -> int:
        existing = target.execute(
            "SELECT id FROM jobs WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cursor = target.execute(
            "INSERT INTO jobs(kind, dedupe_key, payload_json, state, priority, next_run_at, attempts, "
            "max_attempts, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, 0, ?, ?, ?)",
            (
                kind,
                dedupe_key,
                json_dumps(payload or {}),
                priority,
                at,
                max_attempts,
                at,
                at,
            ),
        )
        job_id = int(cursor.lastrowid)
        audit(
            target,
            "system",
            "job.enqueued",
            entity="job",
            entity_id=job_id,
            detail={"kind": kind, "at": at},
        )
        return job_id

    if conn is not None:
        return _insert(conn)
    with db.tx() as own:
        return _insert(own)


def recover_stale_jobs(db: Database, clock: Clock, *, limit: int = 50) -> int:
    """Return expired leases to the queue; bounded by attempts."""
    # 启动时调用：把“running 但租约已过期”的任务找回来。
    # 两种情况：
    #   - 尝试次数还没用完 -> 放回 pending，稍后重跑；
    #   - 已经达到 max_attempts -> 标为 failed，不再无限循环。
    # limit 限制一次回收多少条，避免启动时卡太久。
    now = to_iso(clock.now())
    recovered = 0
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT id, attempts, max_attempts FROM jobs WHERE state='running' AND lease_until < ? LIMIT ?",
            (now, limit),
        ).fetchall()
        for row in rows:
            if int(row["attempts"]) >= int(row["max_attempts"]):
                conn.execute(
                    "UPDATE jobs SET state='failed', lease_owner=NULL, lease_until=NULL, "
                    "last_error='lease expired after max attempts', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET state='pending', lease_owner=NULL, lease_until=NULL, "
                    "last_error='lease expired; requeued', next_run_at=?, updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
            recovered += 1
    return recovered


def claim_job(
    db: Database,
    worker: str,
    clock: Clock,
    *,
    lease_seconds: int = 300,
    kinds: list[str] | None = None,
) -> sqlite3.Row | None:
    # 领任务。整个过程在一个事务里完成，防止两个 worker 同时领到同一条：
    #   1. 按“优先级高、到点早、先入队”挑一条 pending；
    #   2. 立刻改成 running 并写租约；
    #   3. 重新查一次，把最新状态返回给调用方。
    # 为什么 UPDATE 还带 state='pending' 条件？这是“比较并交换”：
    # 如果另一个 worker 已经抢先改了，这次更新影响 0 行，但后面重新查询
    # 会看到最新归属。SQLite 的写锁 + 事务保证不会两边都成功。
    now = clock.now()
    now_iso = to_iso(now)
    lease_until = to_iso(now + timedelta(seconds=lease_seconds))
    with db.tx() as conn:
        params: list[Any] = [now_iso]
        kind_sql = ""
        if kinds:
            kind_sql = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        row = conn.execute(
            "SELECT * FROM jobs WHERE state='pending' AND next_run_at <= ?" + kind_sql +
            " ORDER BY priority DESC, next_run_at ASC, id ASC LIMIT 1",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET state='running', lease_owner=?, lease_until=?, attempts=attempts+1, "
            "updated_at=? WHERE id=? AND state='pending'",
            (worker, lease_until, now_iso, row["id"]),
        )
        fresh = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return fresh


def complete_job(db: Database, job_id: int, clock: Clock, detail: dict | None = None) -> None:
    # 成功完成：清空租约、记录审计。detail 会写进审计日志，方便追溯。
    now = to_iso(clock.now())
    with db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET state='done', lease_owner=NULL, lease_until=NULL, last_error=NULL, "
            "updated_at=? WHERE id=?",
            (now, job_id),
        )
        audit(conn, "worker", "job.done", entity="job", entity_id=job_id, detail=detail or {})


def fail_job(
    db: Database,
    job_id: int,
    error: str,
    clock: Clock,
    *,
    retry_after_seconds: float | None = None,
    terminal: bool = False,
) -> None:
    # 失败处理分两种：
    #   - terminal=True 或次数用完 -> 永久失败；
    #   - 否则放回 pending，并计算下次重试时间。
    # 默认重试间隔是 60 秒、120 秒、240 秒……最多 30 分钟，
    # 避免对已经挂掉的服务疯狂重试。retry_after_seconds 允许调用方
    # 覆盖（例如服务器给了 Retry-After）。
    now = clock.now()
    with db.tx() as conn:
        row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"])
        max_attempts = int(row["max_attempts"])
        if terminal or attempts >= max_attempts:
            conn.execute(
                "UPDATE jobs SET state='failed', lease_owner=NULL, lease_until=NULL, last_error=?, "
                "updated_at=? WHERE id=?",
                (error[:500], to_iso(now), job_id),
            )
        else:
            delay = retry_after_seconds if retry_after_seconds is not None else min(
                1800.0, 60.0 * (2 ** max(0, attempts - 1))
            )
            conn.execute(
                "UPDATE jobs SET state='pending', lease_owner=NULL, lease_until=NULL, last_error=?, "
                "next_run_at=?, updated_at=? WHERE id=?",
                (error[:500], to_iso(now + timedelta(seconds=delay)), to_iso(now), job_id),
            )
        audit(conn, "worker", "job.failed", entity="job", entity_id=job_id, detail={"error": error[:300]})


def job_payload(row: sqlite3.Row) -> dict:
    # 任务参数存在 payload_json 里；解析失败就返回空字典，
    # 让处理函数至少能拿到一个可用的 dict。
    return json_loads(row["payload_json"], {}) or {}


def run_due_jobs(
    db: Database,
    clock: Clock,
    handlers: dict[str, JobHandler],
    *,
    worker: str,
    limit: int = 20,
    lease_seconds: int = 300,
) -> list[dict[str, Any]]:
    # 这是 worker 的主循环体：最多处理 limit 条任务。
    # 关键设计：每条任务的异常都被单独捕获，一条任务失败不会拖垮整个循环。
    # handler 返回字典约定：
    #   {"retry": True, ...} -> 这次算失败，安排重试；
    #   其他返回值            -> 算成功，写入审计。
    results: list[dict[str, Any]] = []
    for _ in range(limit):
        job = claim_job(db, worker, clock, lease_seconds=lease_seconds)
        if job is None:
            break
        kind = str(job["kind"])
        handler = handlers.get(kind)
        if handler is None:
            fail_job(db, int(job["id"]), f"no handler for job kind {kind}", clock, terminal=True)
            results.append({"job_id": job["id"], "kind": kind, "state": "failed"})
            continue
        try:
            outcome = handler(job) or {}
        except Exception as exc:  # noqa: BLE001 - handler failures are isolated per job
            fail_job(db, int(job["id"]), f"{type(exc).__name__}: {exc}", clock)
            results.append({"job_id": job["id"], "kind": kind, "state": "error", "error": str(exc)})
            continue
        if outcome.get("retry"):
            fail_job(
                db,
                int(job["id"]),
                str(outcome.get("error") or "handler requested retry"),
                clock,
                retry_after_seconds=outcome.get("retry_after_seconds"),
                terminal=bool(outcome.get("terminal")),
            )
            results.append({"job_id": job["id"], "kind": kind, "state": "retry"})
        else:
            complete_job(db, int(job["id"]), clock, outcome)
            results.append({"job_id": job["id"], "kind": kind, "state": "done", **outcome})
    return results
