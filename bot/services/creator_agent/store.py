"""SQLite journal for Creator Agent V2 runs.

The journal is append-oriented: a run row, numbered decisions, and immutable
artifact versions.  It never replaces or rewrites the existing drafts,
approvals, or audit tables.
"""

from __future__ import annotations

import json
from typing import Any

from bot.db import Database
from bot.timeutil import to_iso

from .contracts import ArtifactVersion, CreativeBrief, Material


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


class AgentStore:
    def __init__(self, db: Database, clock):
        self.db = db
        self.clock = clock

    def ensure_run(self, brief: CreativeBrief, *, policy_version: str = "creator-v2") -> dict[str, Any]:
        existing = self.get_run(brief.run_key)
        if existing is not None:
            return existing
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO creator_agent_runs(run_key, account_id, origin, generation, status, "
                "brief_json, counters_json, checkpoint_json, policy_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, '{}', NULL, ?, ?, ?)",
                (
                    brief.run_key,
                    brief.account_id,
                    brief.origin,
                    int(brief.generation),
                    _json(brief.as_dict()),
                    policy_version,
                    now,
                    now,
                ),
            )
        return self.get_run(brief.run_key)  # type: ignore[return-value]

    def get_run(self, run_key: str) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM creator_agent_runs WHERE run_key=?", (run_key,))
        if row is None:
            return None
        result = dict(row)
        result["brief"] = _loads(result.pop("brief_json", "{}"), {})
        result["counters"] = _loads(result.pop("counters_json", "{}"), {})
        result["checkpoint"] = _loads(result.pop("checkpoint_json", None), {})
        return result

    def update_run(
        self,
        run_key: str,
        *,
        status: str | None = None,
        counters: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        sets = ["updated_at=?"]
        values: list[Any] = [to_iso(self.clock.now())]
        if status is not None:
            sets.append("status=?")
            values.append(status)
        if counters is not None:
            sets.append("counters_json=?")
            values.append(_json(counters))
        if checkpoint is not None:
            sets.append("checkpoint_json=?")
            values.append(_json(checkpoint))
        values.append(run_key)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE creator_agent_runs SET {', '.join(sets)} WHERE run_key=?", values
            )

    def append_step(
        self,
        run_key: str,
        *,
        action: str,
        args: dict[str, Any],
        intent_summary: str = "",
        expected_result: str = "",
        result: dict[str, Any] | None = None,
        status: str = "ok",
        idempotency_key: str = "",
    ) -> int:
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            seq = int(
                conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM creator_agent_steps WHERE run_key=?",
                    (run_key,),
                ).fetchone()[0]
            ) + 1
            conn.execute(
                "INSERT INTO creator_agent_steps(run_key, seq, action, args_json, intent_summary, "
                "expected_result, result_json, status, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_key,
                    seq,
                    action,
                    _json(args),
                    intent_summary[:500],
                    expected_result[:500],
                    _json(result or {}),
                    status,
                    idempotency_key,
                    now,
                ),
            )
        return seq

    def update_step(
        self,
        run_key: str,
        seq: int,
        *,
        result: dict[str, Any],
        status: str,
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE creator_agent_steps SET result_json=?, status=? WHERE run_key=? AND seq=?",
                (_json(result), status, run_key, int(seq)),
            )

    def steps(self, run_key: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM creator_agent_steps WHERE run_key=? ORDER BY seq LIMIT ?",
            (run_key, int(limit)),
        )
        out = []
        for row in rows:
            item = dict(row)
            item["args"] = _loads(item.pop("args_json", "{}"), {})
            item["result"] = _loads(item.pop("result_json", "{}"), {})
            out.append(item)
        return out

    def save_artifact(self, artifact: ArtifactVersion) -> None:
        """Persist one artifact version without mutating its approved content.

        Content, claims and the brief binding are immutable for a given
        ``(run_key, version)``.  Rendering and submission only update the image
        list, lifecycle status and draft pointer.  A conflicting write raises
        instead of silently replacing the object an approval may bind to.
        """
        content_json = _json(
            {
                "title": artifact.title,
                "body": artifact.body,
                "tags": artifact.tags,
                "content": artifact.content,
            }
        )
        claims_json = _json([c.as_dict() for c in artifact.claims])
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM creator_agent_artifacts WHERE run_key=? AND version=?",
                (artifact.run_key, int(artifact.version)),
            ).fetchone()
            if existing is not None:
                old_content = _loads(existing["content_json"], {})
                old_payload = _json(
                    {
                        "title": old_content.get("title", ""),
                        "body": old_content.get("body", ""),
                        "tags": list(old_content.get("tags") or []),
                        "content": old_content.get("content") or {},
                    }
                )
                if old_payload != content_json or _loads(existing["claims_json"], []) != _loads(
                    claims_json, []
                ):
                    raise RuntimeError(
                        f"artifact v{artifact.version} content/claims are immutable"
                    )
                conn.execute(
                    "UPDATE creator_agent_artifacts SET draft_id=?, images_json=?, status=?, "
                    "brief_hash=? WHERE run_key=? AND version=?",
                    (
                        artifact.draft_id,
                        _json(artifact.images),
                        artifact.status,
                        artifact.brief_hash,
                        artifact.run_key,
                        int(artifact.version),
                    ),
                )
                return
            conn.execute(
                "INSERT INTO creator_agent_artifacts(run_key, version, draft_id, content_json, "
                "images_json, claims_json, status, brief_hash, parent_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.run_key,
                    int(artifact.version),
                    artifact.draft_id,
                    content_json,
                    _json(artifact.images),
                    claims_json,
                    artifact.status,
                    artifact.brief_hash,
                    artifact.parent_version,
                    artifact.created_at,
                ),
            )

    def latest_artifact(self, run_key: str) -> ArtifactVersion | None:
        row = self.db.fetchone(
            "SELECT * FROM creator_agent_artifacts WHERE run_key=? ORDER BY version DESC LIMIT 1",
            (run_key,),
        )
        return self._artifact(row) if row is not None else None

    def artifact(self, run_key: str, version: int) -> ArtifactVersion | None:
        row = self.db.fetchone(
            "SELECT * FROM creator_agent_artifacts WHERE run_key=? AND version=?",
            (run_key, int(version)),
        )
        return self._artifact(row) if row is not None else None

    def _artifact(self, row) -> ArtifactVersion:
        content = _loads(row["content_json"], {})
        from .contracts import ClaimAssessment

        claims = []
        for raw in _loads(row["claims_json"], []):
            if isinstance(raw, dict):
                try:
                    claims.append(ClaimAssessment(**raw))
                except TypeError:
                    continue
        return ArtifactVersion(
            run_key=str(row["run_key"]),
            version=int(row["version"]),
            title=str(content.get("title") or ""),
            body=str(content.get("body") or ""),
            tags=list(content.get("tags") or []),
            images=list(_loads(row["images_json"], [])),
            claims=claims,
            content=dict(content.get("content") or {}),
            status=str(row["status"]),
            brief_hash=str(row["brief_hash"]),
            parent_version=row["parent_version"],
            draft_id=row["draft_id"],
            created_at=str(row["created_at"]),
        )

    def save_material(self, run_key: str, material: Material, *, content: str = "") -> None:
        """Persist fetched material so a restarted runtime need not refetch it."""
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO creator_agent_materials(run_key, material_id, url, title, role, "
                "published_at, fetched_at, content_type, content_hash, quote, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_key, material_id) DO UPDATE SET "
                "url=excluded.url, title=excluded.title, role=excluded.role, "
                "published_at=excluded.published_at, content_hash=excluded.content_hash, "
                "quote=excluded.quote, content=excluded.content",
                (
                    run_key, material.material_id, material.url, material.title,
                    material.role, material.published_at, material.fetched_at,
                    material.content_type, material.content_hash, material.quote,
                    content, now,
                ),
            )

    def materials(self, run_key: str) -> list[Material]:
        rows = self.db.fetchall(
            "SELECT * FROM creator_agent_materials WHERE run_key=? ORDER BY material_id",
            (run_key,),
        )
        out: list[Material] = []
        for row in rows:
            try:
                out.append(Material(
                    material_id=str(row["material_id"]), url=str(row["url"]),
                    title=str(row["title"] or ""), role=str(row["role"] or "unknown"),
                    published_at=row["published_at"], fetched_at=row["fetched_at"],
                    content_type=str(row["content_type"] or "body"),
                    content_hash=str(row["content_hash"] or ""),
                    quote=str(row["quote"] or ""), article_id="",
                ))
            except TypeError:
                continue
        return out

    def material_body(self, run_key: str, material_id: str) -> str:
        row = self.db.fetchone(
            "SELECT content FROM creator_agent_materials WHERE run_key=? AND material_id=?",
            (run_key, material_id),
        )
        return str(row["content"] or "") if row is not None else ""

    def add_preference(
        self,
        *,
        account_id: str,
        scope: str,
        text: str,
        source_run_key: str | None = None,
        source_message_id: str | None = None,
        confidence: float = 0.5,
    ) -> int:
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO creator_agent_preferences(account_id, scope, text, source_run_key, "
                "source_message_id, confidence, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (account_id, scope, text, source_run_key, source_message_id, float(confidence), now, now),
            )
            return int(cur.lastrowid)

    def preferences(self, *, account_id: str, scope: str = "", limit: int = 20) -> list[dict[str, Any]]:
        if scope:
            rows = self.db.fetchall(
                "SELECT * FROM creator_agent_preferences WHERE account_id=? AND active=1 AND scope=? "
                "ORDER BY id DESC LIMIT ?",
                (account_id, scope, int(limit)),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM creator_agent_preferences WHERE account_id=? AND active=1 "
                "ORDER BY id DESC LIMIT ?",
                (account_id, int(limit)),
            )
        return [dict(r) for r in rows]
