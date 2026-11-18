"""Durable state for publication tasks (SQLite, no ORM)."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from ...db import Database, json_dumps, json_loads
from ...timeutil import Clock, parse_iso, to_iso
from .contracts import PublicationTask


def _code() -> str:
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    return "PB-" + "".join(secrets.choice(alphabet) for _ in range(7))


def _work_key(account_id: str, approval: Any, draft: Any, images: list[dict[str, Any]]) -> str:
    """Semantic identity of the work, independent of the approval sheet code."""
    payload = {
        "platform": "xiaohongshu",
        "account_id": str(account_id),
        "draft_id": int(draft["id"]),
        "draft_version": int(draft["version"]),
        "title": str(draft["title"]),
        "body_sha256": str(approval["body_sha256"]),
        "images_sha256": str(approval["images_sha256"]),
        "images": [
            {"ordinal": int(i["ordinal"]), "sha256": str(i["sha256"])}
            for i in sorted(images, key=lambda x: int(x["ordinal"]))
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _hash_package(package: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _task(row: Any) -> PublicationTask:
    return PublicationTask(
        id=int(row["id"]),
        code=str(row["code"]),
        account_id=str(row["account_id"]),
        approval_id=int(row["approval_id"]),
        draft_id=int(row["draft_id"]),
        draft_version=int(row["draft_version"]),
        package_hash=str(row["approved_package_hash"]),
        work_key=row["work_key"],
        generation=int(row["generation"]),
        state=str(row["state"]),
        cancel_requested=bool(row["cancel_requested"]),
        creator_run_key=row["creator_run_key"],
        next_wake_at=row["next_wake_at"],
        wait_reason=row["wait_reason"],
        active_attempt_id=row["active_attempt_id"],
        last_error_class=row["last_error_class"],
        retry_count=int(row["retry_count"] or 0),
        lease_owner=row["lease_owner"],
        lease_until=row["lease_until"],
        terminal_evidence_ref=row["terminal_evidence_ref"],
    )


class PublicationStore:
    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.clock = clock

    def freeze_package(
        self,
        *,
        approval: Any,
        draft: Any,
        images: list[dict[str, Any]],
        account_id: str,
        creator_run_key: str | None = None,
        visibility: str = "public",
    ) -> dict[str, Any]:
        return {
            "platform": "xiaohongshu",
            "account_id": account_id,
            "approval_id": int(approval["id"]),
            "approval_code": str(approval["code"]),
            "draft_id": int(draft["id"]),
            "draft_version": int(draft["version"]),
            "title": str(draft["title"]),
            "body": str(draft["body"]),
            "tags": list((json_loads(draft["body_json"], {}) or {}).get("tags") or []),
            "body_sha256": str(approval["body_sha256"]),
            "images_sha256": str(approval["images_sha256"]),
            "images": [
                {
                    "ordinal": int(i["ordinal"]),
                    "path": str(i["path"]),
                    "sha256": str(i["sha256"]),
                }
                for i in sorted(images, key=lambda x: int(x["ordinal"]))
            ],
            "visibility": visibility,
            "scheduled_at": str(approval["scheduled_at"]),
            "expires_at": str(approval["expires_at"]),
            "creator_run_key": creator_run_key,
        }

    def create_task(
        self,
        *,
        approval: Any,
        draft: Any,
        images: list[dict[str, Any]],
        account_id: str,
        creator_run_key: str | None = None,
        asset_roots: list[str] | None = None,
        asset_backup_root: str | None = None,
    ) -> PublicationTask:
        package = self.freeze_package(
            approval=approval,
            draft=draft,
            images=images,
            account_id=account_id,
            creator_run_key=creator_run_key,
        )
        package_hash = _hash_package(package)
        work_key = _work_key(account_id, approval, draft, images)
        # Program-owned immutable backup manifest. The model never chooses
        # backup paths; a missing source is recorded and restore must wait.
        try:
            from .diagnostics import ensure_asset_manifest
            ensure_asset_manifest(
                package,
                target_roots=asset_roots,
                backup_root=asset_backup_root or "var/backups/publication_assets",
            )
        except Exception:
            pass
        now = to_iso(self.clock.now())
        task_code = _code()
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM publication_tasks WHERE work_key=? AND state != 'cancelled' "
                "ORDER BY id DESC LIMIT 1",
                (work_key,),
            ).fetchone()
            if existing is not None:
                if int(existing["approval_id"]) == int(approval["id"]):
                    return _task(existing)
                raise ValueError(
                    "this work already has a publication task; a different approval cannot "
                    "create a parallel send"
                )
            same_approval = conn.execute(
                "SELECT * FROM publication_tasks WHERE approval_id=? AND approved_package_hash=?",
                (int(approval["id"]), package_hash),
            ).fetchone()
            if same_approval is not None:
                return _task(same_approval)
            cur = conn.execute(
                "INSERT INTO publication_tasks(code,account_id,approval_id,draft_id,draft_version,"
                "creator_run_key,approved_package_json,approved_package_hash,work_key,generation,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,'ready',?,?)",
                (
                    task_code,
                    account_id,
                    int(approval["id"]),
                    int(draft["id"]),
                    int(draft["version"]),
                    creator_run_key,
                    json_dumps(package),
                    package_hash,
                    work_key,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM publication_tasks WHERE id=?", (cur.lastrowid,)).fetchone()
            return _task(row)

    def get(self, task_id: int | None = None, code: str | None = None) -> PublicationTask | None:
        if task_id is None and code is None:
            raise ValueError("task_id or code is required")
        row = self.db.fetchone(
            "SELECT * FROM publication_tasks WHERE id=? OR code=? ORDER BY id DESC LIMIT 1"
            if task_id is not None and code is not None
            else ("SELECT * FROM publication_tasks WHERE id=?" if task_id is not None else "SELECT * FROM publication_tasks WHERE code=?"),
            ((task_id, code) if task_id is not None and code is not None else (task_id if task_id is not None else code,)),
        )
        return _task(row) if row is not None else None

    def by_approval(self, approval_id: int) -> PublicationTask | None:
        row = self.db.fetchone(
            "SELECT * FROM publication_tasks WHERE approval_id=? ORDER BY id DESC LIMIT 1",
            (int(approval_id),),
        )
        return _task(row) if row is not None else None

    def package(self, task: PublicationTask) -> dict[str, Any]:
        row = self.db.fetchone("SELECT approved_package_json FROM publication_tasks WHERE id=?", (task.id,))
        return json_loads(row["approved_package_json"], {}) if row is not None else {}

    def record_pre_send_baseline(self, attempt_id: int, baseline: dict[str, Any]) -> None:
        """Persist the account/feed snapshot taken immediately before POST.

        This row outlives the process.  Reconciliation must never use a
        pre-existing same-title note as success evidence after a restart.
        """
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE publish_attempts SET pre_send_baseline_json=?, updated_at=? WHERE id=?",
                (json_dumps(baseline), now, int(attempt_id)),
            )

    def update(
        self,
        task_id: int,
        *,
        expected_generation: int | None = None,
        state: str | None = None,
        cancel_requested: bool | None = None,
        next_wake_at: str | None = None,
        wait_reason: str | None = None,
        active_attempt_id: int | None = None,
        last_error_class: str | None = None,
        retry_count: int | None = None,
        lease_owner: str | None = None,
        lease_until: str | None = None,
        terminal_evidence_ref: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> PublicationTask:
        fields: list[str] = []
        values: list[Any] = []
        for key, value in (
            ("state", state),
            ("cancel_requested", int(cancel_requested) if cancel_requested is not None else None),
            ("next_wake_at", next_wake_at),
            ("wait_reason", wait_reason),
            ("active_attempt_id", active_attempt_id),
            ("last_error_class", last_error_class),
            ("retry_count", retry_count),
            ("lease_owner", lease_owner),
            ("lease_until", lease_until),
            ("terminal_evidence_ref", terminal_evidence_ref),
        ):
            if value is not None:
                fields.append(f"{key}=?")
                values.append(value)
        fields.append("updated_at=?")
        values.append(to_iso(self.clock.now()))
        sql = f"UPDATE publication_tasks SET {', '.join(fields)} WHERE id=?"
        params = values + [task_id]
        if expected_generation is not None:
            sql += " AND generation=?"
            params.append(expected_generation)

        def _apply(target: sqlite3.Connection) -> PublicationTask:
            cur = target.execute(sql, params)
            if state is not None and state not in {"waiting_owner", "waiting_login", "waiting_approval"}:
                target.execute(
                    "UPDATE publication_owner_requests SET state='closed', updated_at=? "
                    "WHERE task_id=? AND state='open'",
                    (to_iso(self.clock.now()), task_id),
                )
            if cur.rowcount != 1:
                raise RuntimeError("publication task CAS conflict")
            row = target.execute(
                "SELECT * FROM publication_tasks WHERE id=?", (task_id,)
            ).fetchone()
            return _task(row)

        if conn is not None:
            return _apply(conn)
        with self.db.tx() as own:
            return _apply(own)

    def acquire_lease(self, task_id: int, owner: str, *, seconds: int = 120) -> bool:
        """Acquire an exclusive short lease; stale leases expire by time."""
        now = self.clock.now()
        until = now + timedelta(seconds=max(10, int(seconds)))
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE publication_tasks SET lease_owner=?, lease_until=?, updated_at=? "
                "WHERE id=? AND (lease_until IS NULL OR lease_until < ?)",
                (owner, to_iso(until), to_iso(now), int(task_id), to_iso(now)),
            )
            return cur.rowcount == 1

    def release_lease(self, task_id: int, owner: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE publication_tasks SET lease_owner=NULL, lease_until=NULL, updated_at=? "
                "WHERE id=? AND lease_owner=?",
                (to_iso(self.clock.now()), int(task_id), owner),
            )

    def bump_retry(self, task_id: int, *, expected_generation: int | None = None) -> PublicationTask:
        with self.db.tx() as conn:
            sql = (
                "UPDATE publication_tasks SET retry_count=retry_count+1, updated_at=? "
                "WHERE id=?"
            )
            params: list[Any] = [to_iso(self.clock.now()), task_id]
            if expected_generation is not None:
                sql += " AND generation=?"
                params.append(expected_generation)
            cur = conn.execute(sql, params)
            if cur.rowcount != 1:
                raise RuntimeError("publication task CAS conflict")
            row = conn.execute("SELECT * FROM publication_tasks WHERE id=?", (task_id,)).fetchone()
            return _task(row)

    def add_step(
        self,
        task_id: int,
        *,
        action: str,
        args: dict[str, Any],
        intent_summary: str,
        observation: dict[str, Any] | None,
        evidence_refs: list[str],
        status: str,
    ) -> int:
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            seq = int(conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM publication_agent_steps WHERE task_id=?", (task_id,)).fetchone()[0])
            conn.execute(
                "INSERT INTO publication_agent_steps(task_id,seq,action,args_json,intent_summary,"
                "observation_json,evidence_refs_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, seq, action, json_dumps(args), intent_summary, json_dumps(observation or {}), json_dumps(evidence_refs), status, now),
            )
            return seq

    def steps(self, task_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM publication_agent_steps WHERE task_id=? ORDER BY seq DESC LIMIT ?", (task_id, limit)
        )
        return [dict(r) for r in reversed(rows)]

    def batch_start(self, task_id: int, *, force: bool = False) -> int:
        """Start an owner-authorized budget batch once.

        A batch marker is durable; automatic restarts do not create a new one.
        Only an explicit owner resume/login/repair command calls this method.
        """
        row = self.db.fetchone(
            "SELECT MAX(seq) AS seq FROM publication_agent_steps "
            "WHERE task_id=? AND action='batch_start'",
            (int(task_id),),
        )
        if not force and row is not None and row["seq"] is not None:
            return int(row["seq"])
        return self.add_step(
            task_id, action="batch_start", args={},
            intent_summary="owner-authorized publication budget batch",
            observation={}, evidence_refs=[], status="ok",
        )

    def batch_usage(self, task_id: int) -> tuple[int, int]:
        """Return (model transport attempts, decision steps) in this batch."""
        row = self.db.fetchone(
            "SELECT MAX(seq) AS seq FROM publication_agent_steps "
            "WHERE task_id=? AND action='batch_start'",
            (int(task_id),),
        )
        since = int(row["seq"] or 0) if row is not None else 0
        calls = self.db.scalar(
            "SELECT COUNT(*) FROM publication_agent_steps "
            "WHERE task_id=? AND seq>? AND action='model_attempt'",
            (int(task_id), since),
        )
        decisions = self.db.scalar(
            "SELECT COUNT(*) FROM publication_agent_steps "
            "WHERE task_id=? AND seq>? AND action='model_decision'",
            (int(task_id), since),
        )
        return int(calls or 0), int(decisions or 0)

    def reserve_model_attempt(self, task_id: int, *, max_batch: int, max_daily: int) -> int:
        """Persist a transport-attempt reservation before the network call.

        The check and insert happen in one transaction, so concurrent workers
        cannot both consume the last slot.
        """
        from .contracts import PublicationBudgetExceeded

        now = self.clock.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT MAX(seq) AS seq FROM publication_agent_steps "
                "WHERE task_id=? AND action='batch_start'",
                (int(task_id),),
            ).fetchone()
            since = int(row["seq"] or 0) if row is not None else 0
            batch = int(conn.execute(
                "SELECT COUNT(*) FROM publication_agent_steps "
                "WHERE task_id=? AND seq>? AND action='model_attempt'",
                (int(task_id), since),
            ).fetchone()[0])
            daily = int(conn.execute(
                "SELECT COUNT(*) FROM publication_agent_steps "
                "WHERE action='model_attempt' AND created_at>=?",
                (to_iso(day_start),),
            ).fetchone()[0])
            if max_batch >= 0 and batch >= max_batch:
                raise PublicationBudgetExceeded(f"batch model budget exhausted ({batch}/{max_batch})")
            if max_daily >= 0 and daily >= max_daily:
                raise PublicationBudgetExceeded(f"daily model budget exhausted ({daily}/{max_daily})")
            seq = int(conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM publication_agent_steps WHERE task_id=?",
                (int(task_id),),
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO publication_agent_steps(task_id,seq,action,args_json,intent_summary,"
                "observation_json,evidence_refs_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(task_id), seq, "model_attempt", "{}", "reserved before transport",
                 "{}", "[]", "reserved", to_iso(now)),
            )
            return seq

    def add_owner_request(self, task_id: int, *, question: str, kind: str, expires_at: str | None = None) -> int:
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO publication_owner_requests(task_id,question,kind,state,expires_at,created_at,updated_at) "
                "VALUES (?,?,?,'open',?,?,?)",
                (task_id, question, kind, expires_at, now, now),
            )
            return int(cur.lastrowid)

    def open_owner_request(self, task_id: int) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM publication_owner_requests WHERE task_id=? AND state='open' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        if row is None:
            return None
        expires = parse_iso(row["expires_at"]) if row["expires_at"] else None
        if expires is not None and expires <= self.clock.now():
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE publication_owner_requests SET state='expired', updated_at=? "
                    "WHERE id=? AND state='open'",
                    (to_iso(self.clock.now()), int(row["id"])),
                )
            return None
        return dict(row)
