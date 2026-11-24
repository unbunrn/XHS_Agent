"""Live runtime for the publication recovery agent.

The runner only knows this small interface. Every side effect still goes
through the existing approval/publish gate; the runtime never bypasses it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from ...adapters.publishers import Publisher, build_publisher
from ...jobs import enqueue_job
from ...services import notifications
from ...services.publish import publish_approval
from ...timeutil import parse_iso, to_iso
from .store import PublicationStore


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class LivePublicationRuntime:
    def __init__(self, db, config, clock, *, publisher: Publisher | None = None,
                 store: PublicationStore | None = None):
        self.db = db
        self.config = config
        self.clock = clock
        self._publisher = publisher
        self.store = store or PublicationStore(db, clock)

    @property
    def publisher(self) -> Publisher:
        if self._publisher is None:
            self._publisher = build_publisher(self.config, self.clock)
        return self._publisher

    def bridge_health(self) -> dict[str, Any]:
        try:
            value = self.publisher.health()
            return value if isinstance(value, dict) else {"ok": bool(value)}
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def login_check(self) -> dict[str, Any]:
        try:
            login = self.publisher._login_check()  # type: ignore[attr-defined]
            ident = self.publisher._identity_check()  # type: ignore[attr-defined]
            return {"ok": bool(login[0] and ident[0]), "login": list(login), "identity": list(ident)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def inspect_failure(self, task_id: int) -> dict[str, Any]:
        row = self.db.fetchone(
            "SELECT id,attempt_no,state,error,dispatch_state,external_post_id,created_at,finished_at "
            "FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return dict(row) if row is not None else {}

    def publish(self, task, package: dict[str, Any], *, retry_basis: str = "") -> dict[str, Any]:
        approval = self.db.fetchone("SELECT * FROM approvals WHERE id=?", (task.approval_id,))
        if approval is None:
            return {"ok": False, "state": "missing_approval", "detail": "approval disappeared"}
        try:
            result = publish_approval(
                self.db,
                self.config,
                self.clock,
                int(task.approval_id),
                publisher=self.publisher,
                task_id=task.id,
                attempt_no=int(task.retry_count) + 1,
                allow_reopen=True,
                retry_basis=retry_basis,
                task_generation=int(task.generation),
                package_hash=str(task.package_hash),
                lease_owner=task.lease_owner,
                before_send=self._capture_pre_send_baseline,
            )
        except Exception as exc:  # noqa: BLE001 - runner records the observation
            return {"ok": False, "state": "exception", "detail": f"{type(exc).__name__}: {exc}"}
        if not result.get("ok") and result.get("state") == "failed":
            failure_class = str(result.get("failure_class") or "")
            if not bool(result.get("retryable")):
                self.store.update(
                    task.id,
                    state="waiting_owner",
                    wait_reason=(
                        f"publish failed without safe retry ({failure_class or 'unknown'}); "
                        "owner or repair action required"
                    ),
                )
                self.notify(
                    task,
                    f"发布任务 {task.code} 未能安全自动重试（{failure_class or 'unknown'}），"
                    "已停止并等待人工处理。",
                )
                return result
            self.store.bump_retry(task.id)
            fresh = self.store.get(task_id=task.id)
            retries = int(fresh.retry_count) if fresh is not None else 1
            max_retries = int(self.config.get("publication_agent.max_retries", 3))
            if retries <= max_retries:
                delay = min(
                    int(self.config.get("publication_agent.retry_max_seconds", 1800)),
                    int(self.config.get("publication_agent.retry_base_seconds", 30))
                    * (2 ** max(0, retries - 1)),
                )
                wake = self.clock.now() + timedelta(seconds=delay)
                self.store.update(
                    task.id,
                    state="retry_wait",
                    next_wake_at=to_iso(wake),
                    wait_reason=f"retry {retries}/{max_retries} after publish failure",
                )
                self.schedule(task.id, wake)
            else:
                self.store.update(
                    task.id,
                    state="waiting_owner",
                    wait_reason="publish retry limit reached; owner decision required",
                )
                self.notify(task, f"发布任务 {task.code} 连续失败 {retries} 次，已停止自动重试，请主人决定。")
        elif result.get("state") == "unknown":
            # Persist the error class and queue the same durable reconciliation
            # path used by an owner "修复并重试".  The task update and the job
            # insert share one transaction: a crash cannot leave "unknown
            # recorded but no reconciliation job".
            attempt_id = int(result.get("attempt_id") or 0)
            if not attempt_id:
                row = self.db.fetchone(
                    "SELECT id FROM publish_attempts WHERE publication_task_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (task.id,),
                )
                attempt_id = int(row["id"]) if row is not None else 0
            if attempt_id:
                from .entry import wake_unknown_reconciliation

                with self.db.tx() as conn:
                    self.store.update(
                        task.id,
                        state="waiting_owner",
                        last_error_class="unknown",
                        wait_reason="publish outcome unknown; automatic reconciliation queued",
                        conn=conn,
                    )
                    wake_unknown_reconciliation(
                        self.db, self.clock, task.id, attempt_id, conn=conn,
                    )
            else:
                self.store.update(
                    task.id,
                    state="waiting_owner",
                    last_error_class="unknown",
                    wait_reason="publish outcome unknown; manual reconciliation required",
                )
        elif not result.get("ok"):
            self.store.update(
                task.id,
                state="waiting_owner",
                wait_reason=str(result.get("detail") or result.get("state") or "publication blocked"),
            )
        return result

    def check_result(self, task) -> dict[str, Any]:
        row = self.db.fetchone(
            "SELECT * FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
            (task.id,),
        )
        if row is None:
            return {"state": "no_attempt"}
        state = str(row["state"])
        if state == "published":
            self.store.update(task.id, state="published", terminal_evidence_ref=str(row["id"]))
        elif state == "simulated":
            self.store.update(task.id, state="simulated", terminal_evidence_ref=str(row["id"]))
            return {"state": "simulated", "detail": "mock publication recorded separately from published"}
        elif state in {"unknown", "publishing"}:
            # Try one read-only platform reconciliation before asking the owner.
            # A readable feed with no matching note stays unknown: absence from
            # the newest page is not proof that the post does not exist.
            reconciled = self._reconcile_from_feed(task, row)
            if reconciled is not None and reconciled.get("state") == "published":
                return reconciled
            self.store.update(
                task.id,
                state="waiting_owner",
                wait_reason=(
                    "publish outcome unknown; manual reconciliation required"
                    if state == "unknown"
                    else "publish attempt was in progress during restart; check the platform before retrying"
                ),
            )
        return {
            "state": state,
            "attempt_id": int(row["id"]),
            "external_post_id": row["external_post_id"],
            "external_url": row["external_url"],
            "error": row["error"],
        }

    def _read_verified_feed(self) -> dict[str, Any]:
        """Read notes and the identity that owns them in one trusted result."""
        checked = getattr(self.publisher, "lookup_published_notes_verified", None)
        if callable(checked):
            try:
                report = checked(limit=20)
            except Exception as exc:  # noqa: BLE001 - unreadable feed is not proof
                return {"ok": False, "read_ok": False, "identity_ok": False,
                        "account_identity": None, "notes": [],
                        "reason": f"{type(exc).__name__}: {exc}"}
            if not isinstance(report, dict):
                return {"ok": False, "read_ok": False, "identity_ok": False,
                        "account_identity": None, "notes": [],
                        "reason": "verified account feed returned no structured result"}
            notes = report.get("notes")
            return {
                "ok": bool(report.get("ok")),
                "read_ok": bool(report.get("read_ok")),
                "identity_ok": bool(report.get("identity_ok")),
                "account_identity": str(report.get("account_identity") or "").strip() or None,
                "notes": list(notes) if isinstance(notes, list) else [],
                "reason": str(report.get("reason") or ""),
            }
        # Legacy adapters did not bind identity to the list.  Their feed may
        # be readable, but it cannot support a title-based success claim.
        try:
            notes = self.publisher.lookup_published_notes(limit=20)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "read_ok": False, "identity_ok": False,
                    "account_identity": None, "notes": [],
                    "reason": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "read_ok": True, "identity_ok": False,
                "account_identity": None, "notes": list(notes or []),
                "reason": "adapter did not bind account identity to the feed"}

    def _capture_pre_send_baseline(self, claim) -> None:
        """Snapshot identity + existing note IDs before the network POST."""
        attempt = claim.attempt
        report = self._read_verified_feed()
        baseline: dict[str, Any] = {
            "read_ok": bool(report.get("read_ok")),
            "identity_ok": bool(report.get("identity_ok")),
            "account_identity": report.get("account_identity"),
            "note_ids": sorted(
                {str(note.get("id")) for note in report.get("notes") or [] if note.get("id")}
            ),
            "captured_at": to_iso(self.clock.now()),
        }
        if report.get("reason"):
            baseline["reason"] = str(report["reason"])
        self.store.record_pre_send_baseline(int(attempt["id"]), baseline)

    def preview_result(self, task) -> dict[str, Any]:
        """Read-only reconciliation preview used by diagnosis.

        Unlike ``check_result`` this never updates the attempt, task,
        approval, draft, or event.  Evidence found here is only an observation
        for the owner; a later authorised reconciliation must apply it.
        """
        row = self.db.fetchone(
            "SELECT * FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
            (task.id,),
        )
        if row is None:
            return {"state": "no_attempt", "preview": True}
        state = str(row["state"])
        if state in {"published", "simulated"}:
            return {"state": state, "attempt_id": int(row["id"]),
                    "external_post_id": row["external_post_id"], "preview": True}
        if state in {"unknown", "publishing"}:
            report = self._reconcile_from_feed(task, row, apply=False) or {}
            report["preview"] = True
            return report
        return {"state": state, "attempt_id": int(row["id"]),
                "external_post_id": row["external_post_id"], "preview": True}

    def _reconcile_from_feed(self, task, row, *, apply: bool = True) -> dict[str, Any] | None:
        report = self._read_verified_feed()
        if not report.get("read_ok"):
            return {
                "state": str(row["state"]),
                "attempt_id": int(row["id"]),
                "reconcile": "feed_unavailable",
                "error": str(report.get("reason") or "account feed unavailable"),
            }
        baseline: dict[str, Any] = {}
        if "pre_send_baseline_json" in row.keys():
            try:
                baseline = json.loads(row["pre_send_baseline_json"] or "{}") or {}
            except (TypeError, ValueError):
                baseline = {}
        baseline_identity = bool(baseline.get("identity_ok"))
        baseline_account = str(baseline.get("account_identity") or "").strip()
        current_identity = bool(report.get("identity_ok"))
        current_account = str(report.get("account_identity") or "").strip()

        wanted_id = str(row["external_post_id"] or "").strip()
        draft = self.db.fetchone(
            "SELECT d.title FROM approvals a JOIN drafts d ON d.id=a.draft_id WHERE a.id=?",
            (int(row["approval_id"]),),
        )
        wanted_title = str(draft["title"] if draft is not None else "").strip()
        account_ok = current_identity
        if account_ok and baseline_account:
            account_ok = bool(current_account) and current_account == baseline_account
        match = None
        if not account_ok:
            match = None
        elif wanted_id:
            # A bound platform id is a separate success path from title
            # inference.  It still has to come from the verified account.
            for note in report.get("notes") or []:
                if str(note.get("id") or "").strip() == wanted_id:
                    match = note
                    break
        elif baseline_identity and baseline_account and baseline.get("read_ok"):
            baseline_ids = {str(value) for value in baseline.get("note_ids") or []}
            candidates = [
                note
                for note in report.get("notes") or []
                if str(note.get("id") or "").strip()
                and str(note.get("id") or "").strip() not in baseline_ids
                and wanted_title
                and str(note.get("title") or "").strip() == wanted_title
            ]
            if len(candidates) == 1:
                match = candidates[0]
        if match is None:
            return {
                "state": str(row["state"]),
                "attempt_id": int(row["id"]),
                "reconcile": (
                    "account_mismatch" if not account_ok else
                    "identity_unverified" if not current_identity else
                    "no_pre_send_baseline" if not wanted_id and not (baseline_identity and baseline_account and baseline.get("read_ok")) else
                    "no_new_matching_note"
                ),
            }
        note_id = str(match.get("id") or "").strip() or None
        note_url = str(match.get("url") or "").strip() or None
        if not apply:
            return {
                "state": "unknown",
                "attempt_id": int(row["id"]),
                "external_post_id": note_id,
                "external_url": note_url,
                "reconcile": "candidate_found",
            }
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            updated = conn.execute(
                "UPDATE publish_attempts SET state='published', external_post_id=?, external_url=?, "
                "reconcile_note='reconciled from authenticated account feed', finished_at=?, updated_at=? "
                "WHERE id=? AND state IN ('unknown','publishing')",
                (note_id, note_url, now, now, int(row["id"])),
            )
            if updated.rowcount != 1:
                return None
            conn.execute(
                "UPDATE publication_tasks SET state='published', active_attempt_id=NULL, "
                "terminal_evidence_ref=?, updated_at=? WHERE id=?",
                (str(row["id"]), now, int(task.id)),
            )
            conn.execute("UPDATE approvals SET status='published' WHERE id=?", (int(row["approval_id"]),))
            conn.execute(
                "UPDATE drafts SET status='published', updated_at=? WHERE id=?",
                (now, int(row["draft_id"])),
            )
            conn.execute(
                "UPDATE events SET status='published' WHERE id=(SELECT event_id FROM drafts WHERE id=?)",
                (int(row["draft_id"]),),
            )
        return {
            "state": "published",
            "attempt_id": int(row["id"]),
            "external_post_id": note_id,
            "external_url": note_url,
            "reconcile": "platform_feed",
        }

    def notify(self, task, text: str) -> int:
        text = str(text or "").strip()
        if not text:
            return 0
        row = self.db.fetchone(
            "SELECT MAX(seq) AS seq FROM publication_agent_steps "
            "WHERE task_id=? AND action='batch_start'",
            (int(task.id),),
        )
        batch = int(row["seq"] or 0) if row is not None else 0
        return notifications.queue_outcome(
            self.db,
            self.config,
            self.clock,
            text,
            dedupe_key=f"publication:{task.id}:batch:{batch}:{_digest(text)}",
        )

    def schedule(
        self, task_id: int, wake: datetime | str, *,
        diagnose_only: bool = False, reconcile_only: bool = False,
    ) -> int:
        when = wake if isinstance(wake, str) else to_iso(wake)
        run_at = parse_iso(when) if isinstance(wake, str) else wake
        suffix = ":diagnose" if diagnose_only else (":reconcile" if reconcile_only else "")
        return enqueue_job(
            self.db,
            "publication_agent",
            f"publication:task:{task_id}:{when}{suffix}",
            payload={
                "task_id": int(task_id),
                "diagnose_only": bool(diagnose_only),
                "reconcile_only": bool(reconcile_only),
            },
            run_at=run_at,
            clock=self.clock,
            priority=3,
            max_attempts=1,
        )
