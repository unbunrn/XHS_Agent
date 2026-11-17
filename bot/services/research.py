"""Source ingestion, event clustering, evidence building and candidate scoring."""

# =============================================================================
# 中文导读：从“一堆新闻”到“一个值得写的选题”
# =============================================================================
# 这个文件是整条流水线的前半段：
#
#   1. sync_sources    把配置文件里的来源同步进数据库；
#   2. ingest_source   抓取一个来源，逐条入库；
#   3. _attach_event   把讲同一件事的多篇文章聚成一个“事件”；
#   4. _evidence_for_item  从文章里摘出证据句子；
#   5. score_event     给事件打分（相关性、实用性、新鲜度、来源多样性...）；
#   6. ensure_research_queue  把高分事件放进“待写稿”队列。
#
# 什么是“事件”？
#   同一件事可能被十家媒体报道。对读者来说这是一条新闻，不是十条。
#   所以系统用标题相似度把它们聚成一个 event，再保留多个来源作证据。
#
# 为什么要打分？
#   不是所有新闻都适合发小红书。太旧、太偏、证据太少、标题太标题党的，
#   分数会低，系统就不会浪费模型调用去写它。
# =============================================================================

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..adapters.sources import SourceAdapter, build_source
from ..adapters.types import NotConfigured, SourceError
from ..audit import audit
from ..config import Config
from ..db import Database, json_dumps, json_loads
from ..httpclient import HttpClient
from ..logging_setup import get_logger
from ..security import (
    SSRFBlocked,
    SSRFGuard,
    canonicalize_url,
    normalize_title_key,
    normalize_ws,
    sha256_text,
    strip_html,
    title_similarity,
    truncate,
)
from ..timeutil import Clock, SystemClock, parse_feed_datetime, to_iso, utcnow_iso
from ..ids import new_id

log = get_logger(__name__)
# 按中文/英文句末标点切句；没有标点时按换行切。
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
# 标题党词汇：出现越多，扣分越多，避免把“震惊体”选进来。
CLICKBAIT = ("震惊", "炸裂", "封神", "吊打", "史诗级", "颠覆", "疯传", "必看", "最后机会")

# A commissioned topic needs enough body material for a grounded draft.  These
# limits deliberately apply only to the targeted path; ordinary ingestion keeps
# its existing two-quote behaviour.
TARGETED_EVIDENCE_LIMIT = 6
_TARGETED_MIN_CHARS = 16
_TARGETED_MAX_CHARS = 260
_TARGETED_TRUNCATE_CHARS = 240
_TOPIC_ASCII_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "the", "to", "with", "about", "after",
    "before", "can", "could", "has", "have", "how", "into", "its", "may",
    "new", "not", "one", "our", "out", "that", "their", "them", "then",
    "these", "they", "this", "two", "use", "using", "was", "were", "what",
    "when", "where", "which", "who", "why", "will", "with", "you", "your",
    "http", "https", "www", "com", "org", "net", "html", "pdf",
}
_TOPIC_CJK_FILLERS = (
    "帮我写一篇", "帮我写", "请帮我写", "写一篇", "写个", "请帮我", "请写",
    "帮我", "生成一篇", "生成", "选题", "关于", "有关", "介绍一下", "介绍",
    "小红书笔记", "小红书", "笔记", "文章", "内容", "要求", "希望", "请",
    "面向", "受众", "读者", "角度", "语气", "风格", "以及", "并且", "然后",
    "可以", "需要", "尽量", "一篇", "这个", "那个",
)
_TARGETED_DETAIL_RE = re.compile(
    r"(定义|指|表示|包括|包含|组成|机制|保存|存储|检索|更新|写入|读取|"
    r"复核|验证|流程|步骤|系统|支持|提供|用于|通过|首先|其次|最后|例如|"
    r"具体|记录|追溯|去重|合并|索引|"
    r"\b(?:defin|refer|means?|includes?|consists?|mechanism|stores?|"
    r"retriev|saves?|updates?|writes?|reads?|offline|review|process|"
    r"steps?|system|supports?|provides?|requires?|records?|index(?:es|ing)?)\w*)",
    re.I,
)
_TARGETED_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_TARGETED_METADATA_RE = re.compile(
    r"^(?:作者|编辑|责编|来源|发布时间|更新时间|日期|时间|阅读时间|字数|标签|"
    r"分类|分享|收藏|原文链接|written\s+by|published|updated|author|date|"
    r"tags?|categor(?:y|ies)|reading\s+time|share)\s*[:：]",
    re.I,
)
_TARGETED_DATE_RE = re.compile(
    r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?:\s+\d{1,2}:\d{2})?$"
)
_TARGETED_READING_RE = re.compile(r"^\d+\s*(?:分钟|min(?:ute)?s?)\s*(?:阅读|read)?$", re.I)


# ---------------------------------------------------------------------------
# Source registry and ingestion
# ---------------------------------------------------------------------------

def sync_sources(db: Database, config: Config) -> int:
    # 配置文件是“来源清单”的唯一来源：
    #   - 新来源 -> 插入；
    #   - 已有来源 -> 更新 url/kind/trust_tier 并重新启用；
    #   - 配置里删掉的来源 -> 只设 enabled=0，不删数据。
    # 为什么不删？历史文章和证据还引用着它，删了会破坏审计链。
    now = utcnow_iso()
    added = 0
    with db.tx() as conn:
        for entry in config.enabled_sources():
            name = str(entry["name"]).strip()
            row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
            kind = str(entry.get("kind", "rss")).lower()
            # Search sources use a configured query rather than a feed URL; keep
            # a stable display value in the schema's url column.
            source_url = str(entry.get("url") or f"deepseek-search://{entry.get('query', '')}")
            if row is None:
                conn.execute(
                    "INSERT INTO sources(name, url, kind, trust_tier, enabled, created_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (name, source_url, kind, int(entry.get("trust_tier", 2)), now),
                )
                added += 1
            else:
                conn.execute(
                    "UPDATE sources SET url=?, kind=?, trust_tier=?, enabled=1 WHERE id=?",
                    (source_url, kind, int(entry.get("trust_tier", 2)), row["id"]),
                )
        configured = {str(e["name"]).strip() for e in config.enabled_sources()}
        # Commissioned sources are owned by durable topic requests, not the
        # recurring-source config. Preserve their state, including explicit disable.
        commissioned = {
            (_topic_source_name(str(r["code"])), "deepseek-search://topic/" + str(r["code"]))
            for r in conn.execute("SELECT code FROM topic_requests").fetchall()
        }
        for row in conn.execute("SELECT id, name, url, kind FROM sources").fetchall():
            if row["kind"] == "deepseek_search" and (row["name"], row["url"]) in commissioned:
                continue
            if row["name"] not in configured:
                conn.execute("UPDATE sources SET enabled=0 WHERE id=?", (row["id"],))
    return added


def source_row(db: Database, name: str) -> sqlite3.Row | None:
    # 按来源名查一行，找不到返回 None。
    return db.fetchone("SELECT * FROM sources WHERE name = ?", (name,))


def is_commissioned_topic_source(source: sqlite3.Row) -> bool:
    """True for the per-request DeepSeek Search rows created by a topic.

    Those rows carry a search query only for the lifetime of one topic job.
    They are intentionally preserved by ``sync_sources`` (including an explicit
    owner disable), but they must never take part in the recurring 30-minute
    collection round: their stored row has no query, so building the adapter
    would raise ``NotConfigured`` and abort every later source in the round.
    """
    if str(source["kind"]).lower() not in {"deepseek_search", "deepseek-search"}:
        return False
    return str(source["url"]).startswith("deepseek-search://topic/")


def recurring_sources(db: Database) -> list[sqlite3.Row]:
    """Enabled sources that belong in the recurring collector.

    Commissioned topic sources are excluded; topic jobs fetch them explicitly.
    """
    rows = db.fetchall("SELECT * FROM sources WHERE enabled=1 ORDER BY id")
    return [row for row in rows if not is_commissioned_topic_source(row)]


def adapter_for(
    db: Database,
    config: Config,
    http: HttpClient,
    clock: Clock,
    source: sqlite3.Row,
    *,
    budget=None,
) -> SourceAdapter:
    # Prefer the live config entry so per-source options (query, allowlist,
    # fetch limits) survive the sources table's narrow columns.
    entry = dict(source)
    for candidate in config.enabled_sources():
        if str(candidate.get("name", "")).strip() == source["name"]:
            entry = dict(candidate)
            break
    entry.update(
        {
            "name": source["name"],
            "kind": source["kind"],
            "trust_tier": source["trust_tier"],
        }
    )
    return build_source(entry, config, http, clock, budget=budget)


def ingest_source(
    db: Database,
    config: Config,
    http: HttpClient,
    clock: Clock,
    source: sqlite3.Row,
    *,
    logger=None,
    budget=None,
) -> dict[str, Any]:
    logger = logger or log
    # 单个来源的失败必须被隔离：一个源挂了，其他源照常采集。
    # 所有异常都在这里被捕获、记录、标记来源失败，然后返回统计结果。
    result: dict[str, Any] = {
        "source": source["name"],
        "entries": 0,
        "new_items": 0,
        "duplicates": 0,
        "new_events": 0,
        "not_modified": False,
        "error": None,
    }
    try:
        # Adapter construction can raise NotConfigured (for example a stale
        # topic-source row with no query).  It must be inside this guard too,
        # otherwise one bad row aborts the whole collection round before the
        # remaining sources are tried.
        adapter = adapter_for(db, config, http, clock, source, budget=budget)
        fetched = adapter.fetch(etag=source["etag"], last_modified=source["last_modified"])
    except (SourceError, NotConfigured) as exc:
        _mark_source_failure(db, source, exc, clock)
        result["error"] = str(exc)
        logger.warning("source %s failed: %s", source["name"], exc)
        return result
    except SSRFBlocked as exc:
        _mark_source_failure(db, source, exc, clock)
        result["error"] = f"blocked: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - a single feed must not kill the round
        _mark_source_failure(db, source, exc, clock)
        result["error"] = f"unexpected: {exc}"
        logger.exception("source %s crashed", source["name"])
        return result

    if fetched.not_modified:
        # 304：内容没变。更新“上次抓取时间”和“下次抓取时间”就够了，
        # 不需要解析，也不会产生新文章。
        with db.tx() as conn:
            conn.execute(
                "UPDATE sources SET last_fetch_at=?, last_status='304', last_error=NULL, fail_count=0, "
                "next_fetch_at=? WHERE id=?",
                (utcnow_iso(), _next_fetch(config, clock, 0).isoformat(), source["id"]),
            )
        result["not_modified"] = True
        return result

    guard = http.guard
    # Fixture links and DeepSeek Search result URLs are never fetched by this
    # process: fixtures are local demo data, and Search/Fetch retrieves the
    # result on the provider side.  Keep the literal private/credential checks
    # but skip local DNS for these two kinds; RSS keeps its full DNS guard.
    kind = str(source["kind"]).lower()
    resolve_links = kind not in {"fixture", "deepseek_search", "deepseek-search"}
    accepted: list[dict[str, Any]] = []
    for entry in fetched.entries:
        url = entry.url.strip()
        try:
            guard.validate(url, allow_private=False, resolve=resolve_links)
        except SSRFBlocked as exc:
            logger.warning("source %s skipped blocked item URL %r: %s", source["name"], url, exc)
            continue
        accepted.append(_ingest_entry(db, config, source, entry, clock))
        result["entries"] += 1

    for item in accepted:
        # 汇总统计，供日志和控制台展示。
        if item.get("status") == "new":
            result["new_items"] += 1
        elif item.get("status") == "duplicate":
            result["duplicates"] += 1
        if item.get("new_event"):
            result["new_events"] += 1

    with db.tx() as conn:
        # 保存 ETag/Last-Modified，下次就能做条件请求；
        # fail_count 归零，表示这个源恢复正常了。
        conn.execute(
            "UPDATE sources SET etag=?, last_modified=?, last_fetch_at=?, last_status=?, last_error=NULL, "
            "fail_count=0, next_fetch_at=? WHERE id=?",
            (
                fetched.etag or source["etag"],
                fetched.last_modified or source["last_modified"],
                utcnow_iso(),
                str(fetched.status),
                _next_fetch(config, clock, 0).isoformat(),
                source["id"],
            ),
        )
    logger.info(
        "source %s: entries=%s new=%s dup=%s events=%s",
        source["name"],
        result["entries"],
        result["new_items"],
        result["duplicates"],
        result["new_events"],
    )
    return result


def _root_raw_item_id(db: Database, raw_item_id: int, *, limit: int = 8) -> int | None:
    """Follow duplicate_of to the first raw item, bounded and cycle-safe."""
    current = int(raw_item_id)
    seen: set[int] = set()
    for _ in range(limit):
        if current in seen:
            return None
        seen.add(current)
        row = db.fetchone("SELECT id, duplicate_of FROM raw_items WHERE id=?", (current,))
        if row is None:
            return None
        parent = row["duplicate_of"]
        if parent is None:
            return int(row["id"])
        current = int(parent)
    return None


def _ingest_entry(
    db: Database,
    config: Config,
    source: sqlite3.Row,
    entry,
    clock: Clock,
    *,
    forced_event_id: int | None = None,
    topic_hint: str | None = None,
    direct_content: bool = False,
) -> dict[str, Any]:
    # 单篇文章入库：
    #   1. 规范化 URL 和标题；
    #   2. 解析发布时间，未来时间不可信 -> 当作未知；
    #   3. 计算内容哈希，用于跨来源去重；
    #   4. 先查“同来源同 URL”，再查“同内容不同来源”；
    #   5. 聚到事件，写入 raw_items；
    #   6. 非重复文章才摘证据。
    canonical = canonicalize_url(entry.url)
    title = normalize_ws(strip_html(entry.title))[:300]
    if not title:
        return {"status": "skipped"}
    summary = normalize_ws(strip_html(entry.summary))[:2000]
    content = normalize_ws(strip_html(entry.content or entry.summary))[:20_000]
    published, precision = parse_feed_datetime(entry.published_raw)
    now = clock.now()
    if published is not None and published > now + timedelta(hours=6):
        # A feed claiming a future publication instant is not trustworthy.
        # 允许 6 小时时区误差；超过就说明时间戳有问题，按未知处理，
        # 免得“未来新闻”永远占据最新位置。
        published, precision = None, "unknown"
    content_hash = sha256_text("\n".join((normalize_title_key(title), summary, content))[:20_000])
    title_key = normalize_title_key(title)
    fetched_at = to_iso(now)

    with db.read() as conn:
        existing = conn.execute(
            "SELECT id, event_id FROM raw_items WHERE source_id=? AND canonical_url=?",
            (source["id"], canonical),
        ).fetchone()
        duplicate = conn.execute(
            "SELECT id, event_id FROM raw_items WHERE content_hash=? AND source_id<>? LIMIT 1",
            (content_hash, source["id"]),
        ).fetchone()
    if existing is not None:
        # 同一来源的同一 URL 已经见过：直接返回，不重复插入。  A topic
        # retry may have crashed after inserting the raw item but before
        # evidence extraction; re-run the idempotent extraction into the
        # request-owned event so recovery cannot strand it without provenance.
        if forced_event_id is not None:
            original_id = _root_raw_item_id(db, int(existing["id"]))
            if original_id is not None:
                _evidence_for_item(
                    db,
                    config,
                    int(forced_event_id),
                    original_id,
                    clock,
                    targeted_hint=topic_hint or "",
                    direct_content=direct_content,
                )
        return {"status": "existing", "id": existing["id"], "event_id": existing["event_id"]}

    duplicate_of = duplicate["id"] if duplicate else None
    if forced_event_id is None:
        event_id, new_event = _attach_event(
            db, config, title=title, summary=summary, canonical=canonical,
            published=published, precision=precision, duplicate_of=duplicate_of, clock=clock,
        )
    else:
        # A user-commissioned topic owns one isolated event.  It never joins the
        # automatic popularity queue and never borrows an unrelated event just
        # because a title happens to look similar.
        event_id = int(forced_event_id)
        new_event = False
        _update_event_times(db, event_id, published, precision, now)
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO raw_items(source_id, url, canonical_url, title, summary, content, author, published_at, "
            "published_precision, fetched_at, content_hash, title_key, event_id, duplicate_of, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source["id"],
                entry.url,
                canonical,
                title,
                summary,
                content,
                entry.author[:120],
                to_iso(published) if published else None,
                precision,
                fetched_at,
                content_hash,
                title_key,
                event_id,
                duplicate_of,
                "duplicate" if duplicate_of else "new",
                fetched_at,
            ),
        )
        raw_item_id = cursor.lastrowid
        audit(
            conn,
            "collector",
            "raw_item.ingested",
            entity="raw_item",
            entity_id=raw_item_id,
            detail={"source": source["name"], "duplicate_of": duplicate_of, "event_id": event_id},
        )
    if duplicate_of is None:
        # 只有“首次出现的内容”才摘证据；转载/重复文章不产生新证据，
        # 否则同一句话会在证据表里出现很多次，虚增证据数量。
        _evidence_for_item(
            db,
            config,
            event_id,
            raw_item_id,
            clock,
            targeted_hint=(topic_hint or "") if forced_event_id is not None else None,
            direct_content=direct_content,
        )
    elif forced_event_id is not None:
        # The commissioned event must still have usable, correctly attributed
        # provenance when automatic collection already saw this exact content.
        # Reuse the original raw item's quotes under the request-owned event.
        # The duplicate raw item keeps duplicate_of, so this is explicitly the
        # same source evidence, not manufactured independent corroboration.
        original_id = _root_raw_item_id(db, int(duplicate_of))
        if original_id is not None:
            _evidence_for_item(
                db,
                config,
                int(forced_event_id),
                original_id,
                clock,
                targeted_hint=topic_hint or "",
                direct_content=direct_content,
            )
    return {
        "status": "duplicate" if duplicate_of else "new",
        "id": raw_item_id,
        "event_id": event_id,
        "new_event": new_event,
    }


def _topic_source_name(code: str) -> str:
    """A deterministic, digit-free display name for a request's source.

    Draft validation requires every number rendered into the body to appear in
    cited evidence.  A request code can contain digits, so encode each code
    character as two letters (injective for 0-9/A-Z).  Retries for the same
    request therefore reuse the same source row and do not create a second
    copy of every raw item.
    """
    encoded: list[str] = []
    for char in str(code):
        if not char.isalnum():
            continue
        value = int(char, 36)
        encoded.append(chr(65 + value // 26) + chr(65 + value % 26))
    return "Topic Search " + "".join(encoded)


def ingest_targeted_entries(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    request_code: str,
    title: str,
    entries,
    topic_hint: str | None = None,
    direct_url: str | None = None,
) -> dict[str, Any]:
    """Ingest provider-retrieved entries into a request-owned event.

    This deliberately skips ``_attach_event`` popularity clustering: the owner
    selected the topic, so it must not be merged into an unrelated highest
    ranked event.  Evidence, age, dedupe, raw-item provenance and source
    validation still follow the ordinary ingestion path.

    ``topic_hint`` selects the commissioned evidence mode: several substantive
    body passages that overlap the owner's topic are retained instead of the
    first heading/introduction pair.  Ordinary automatic ingestion never
    passes this argument and keeps its legacy extraction.
    """
    now = clock.now()
    code = str(request_code or "").strip()
    if not code:
        raise ValueError("request_code is required")
    event_key = "topic_" + hashlib.sha1(code.encode("utf-8")).hexdigest()[:24]
    started = to_iso(now)
    with db.tx() as conn:
        source_name = _topic_source_name(code)
        source = conn.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
        if source is None:
            cur = conn.execute(
                "INSERT INTO sources(name, url, kind, trust_tier, enabled, created_at) "
                "VALUES (?, ?, 'deepseek_search', 2, 1, ?)",
                (source_name, f"deepseek-search://topic/{code}", started),
            )
            source_id = int(cur.lastrowid)
        else:
            source_id = int(source["id"])
            conn.execute("UPDATE sources SET enabled=1 WHERE id=?", (source_id,))
        event = conn.execute("SELECT * FROM events WHERE event_key=?", (event_key,)).fetchone()
        if event is None:
            cur = conn.execute(
                "INSERT INTO events(event_key, title, summary, first_seen_at, last_seen_at, "
                "latest_published_at, earliest_published_at, score, score_detail, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?, 'researched', ?)",
                (
                    event_key,
                    str(title or code)[:300],
                    "用户指定选题（不参与自动热度排序）",
                    started,
                    started,
                    json_dumps({"manual_topic": True, "request_code": code}),
                    started,
                ),
            )
            event_id = int(cur.lastrowid)
        else:
            event_id = int(event["id"])
            conn.execute(
                "UPDATE events SET status='researched', last_seen_at=? WHERE id=?",
                (started, event_id),
            )
        source_row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    results = []
    direct_canonical = canonicalize_url(direct_url) if direct_url else ""
    if source_row is not None:
        for entry in entries:
            is_direct = bool(direct_canonical) and canonicalize_url(entry.url) == direct_canonical
            results.append(
                _ingest_entry(
                    db,
                    config,
                    source_row,
                    entry,
                    clock,
                    forced_event_id=event_id,
                    topic_hint=topic_hint,
                    direct_content=is_direct,
                )
            )
    # S2: propagate the verified source role from the actual fetched material
    # to the evidence rows and then to writer payloads.  Verification is per
    # organization/domain and never inferred from a search scope.  This must run
    # after refresh_targeted_evidence(): that function rebuilds the evidence
    # rows for a retried request, which would otherwise reset these columns.
    # Rebuild rather than merely append.  A retried request may already carry
    # the old two-quote extraction in this request-owned event; without a draft
    # bound to it, those rows are safe to replace with the richer passages.
    # An event that already has a draft is never mutated, so approved/published
    # provenance stays byte-for-byte unchanged.
    refresh_targeted_evidence(
        db,
        config,
        event_id,
        clock,
        topic_hint=topic_hint,
        direct_url=direct_url,
    )
    with db.tx() as conn:
        for entry in entries:
            conn.execute(
                "UPDATE evidence SET source_role=?, source_verified=? "
                "WHERE event_id=? AND source_url=?",
                (
                    str(getattr(entry, "source_role", "unknown") or "unknown")[:40],
                    1 if bool(getattr(entry, "source_verified", False)) else 0,
                    event_id,
                    entry.url,
                ),
            )
    evidence = research_event(db, event_id, clock)
    return {
        "event_id": event_id,
        "source_id": source_id,
        "items": results,
        "evidence": evidence,
        "evidence_ids": [str(item["id"]) for item in evidence],
    }


def _attach_event(
    db: Database,
    config: Config,
    *,
    title: str,
    summary: str,
    canonical: str,
    published: datetime | None,
    precision: str,
    duplicate_of: int | None,
    clock: Clock,
) -> tuple[int, bool]:
    # 事件聚类：
    #   - 只看最近 event_window_days 天内出现过的事件；
    #   - 如果这篇是“转载”，直接跟原文章的事件走；
    #   - 否则找标题最相似的事件，超过阈值就并入；
    #   - 都不匹配就新建事件。
    # 返回 (event_id, 是否新建)。
    now = clock.now()
    window_days = int(config.get("limits.event_window_days", 7))
    cutoff = to_iso(now - timedelta(days=window_days))
    threshold = float(config.get("scoring.title_similarity_threshold", 0.56))
    with db.read() as conn:
        candidates = conn.execute(
            "SELECT id, event_key, title, summary, first_seen_at, last_seen_at, latest_published_at "
            "FROM events WHERE last_seen_at >= ? AND status NOT IN ('rejected')",
            (cutoff,),
        ).fetchall()
        if duplicate_of is not None:
            dup = conn.execute("SELECT event_id FROM raw_items WHERE id=?", (duplicate_of,)).fetchone()
            if dup and dup["event_id"]:
                return int(dup["event_id"]), False
    best = None
    best_sim = 0.0
    for candidate in candidates:
        sim = title_similarity(title, candidate["title"])
        if sim > best_sim:
            best, best_sim = candidate, sim
    if best is not None and best_sim >= threshold:
        event_id = int(best["id"])
        _update_event_times(db, event_id, published, precision, now)
        return event_id, False

    title_key = normalize_title_key(title)
    # event_key 是规范化标题的 SHA-1 前缀。即使相似度没匹配上，
    # 完全相同的标题也会落到同一个 key，靠唯一约束避免重复建事件。
    event_key = "evt_" + hashlib.sha1(title_key.encode("utf-8")).hexdigest()[:24]
    with db.read() as conn:
        existing = conn.execute("SELECT id FROM events WHERE event_key=?", (event_key,)).fetchone()
    if existing is not None:
        _update_event_times(db, int(existing["id"]), published, precision, now)
        return int(existing["id"]), False
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO events(event_key, title, summary, first_seen_at, last_seen_at, latest_published_at, "
            "earliest_published_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                event_key,
                title,
                summary[:2000],
                to_iso(now),
                to_iso(now),
                to_iso(published) if published else None,
                to_iso(published) if published else None,
                to_iso(now),
            ),
        )
        event_id = int(cursor.lastrowid)
        audit(conn, "collector", "event.created", entity="event", entity_id=event_id, detail={"title": title})
    return event_id, True


def _update_event_times(db: Database, event_id: int, published: datetime | None, precision: str, now: datetime) -> None:
    # 事件被再次提及时，更新“最后见到时间”，并维护最早/最晚发布时间。
    # 只比较 ISO 字符串就够了，因为项目里统一用 UTC ISO 格式。
    with db.tx() as conn:
        row = conn.execute(
            "SELECT latest_published_at, earliest_published_at FROM events WHERE id=?", (event_id,)
        ).fetchone()
        latest = _max_iso(row["latest_published_at"], to_iso(published) if published else None)
        earliest = _min_iso(row["earliest_published_at"], to_iso(published) if published else None)
        conn.execute(
            "UPDATE events SET last_seen_at=?, latest_published_at=?, earliest_published_at=? WHERE id=?",
            (to_iso(now), latest, earliest, event_id),
        )


def _evidence_for_item(
    db: Database,
    config: Config,
    event_id: int,
    raw_item_id: int,
    clock: Clock,
    *,
    targeted_hint: str | None = None,
    direct_content: bool = False,
) -> int:
    # 从一篇文章里摘证据，写入 evidence 表。
    # INSERT OR IGNORE + 唯一约束 (event_id, raw_item_id, quote) 保证
    # 同一篇文章的同一句话不会重复入库。
    with db.read() as conn:
        row = conn.execute(
            "SELECT raw.title, raw.summary, raw.content, raw.published_at, raw.published_precision, "
            "raw.fetched_at, raw.url, s.name AS source_name, s.trust_tier "
            "FROM raw_items raw JOIN sources s ON s.id=raw.source_id WHERE raw.id=?",
            (raw_item_id,),
        ).fetchone()
    if row is None:
        return 0
    if direct_content:
        # The owner supplied this exact URL.  Its identity, not lexical overlap
        # with the chat text, is the relevance proof; still cite only substantive
        # fetched body sentences, never the search summary or navigation labels.
        quotes = _pick_direct_quotes(row["content"])
    elif targeted_hint is None:
        quotes = _pick_quotes(row["title"], row["summary"], row["content"])
    else:
        # The owner's topic hint is combined with the article's own title and
        # URL path.  Those are metadata for locating relevant passages, not
        # evidence: the emitted quotes still come only from raw content.
        hint = " ".join(
            part
            for part in (
                str(row["title"] or ""),
                urlsplit(str(row["url"] or "")).path,
                str(targeted_hint or ""),
            )
            if part
        )
        quotes = _pick_quotes(
            row["title"],
            row["summary"],
            row["content"],
            limit=TARGETED_EVIDENCE_LIMIT,
            topic_hint=hint,
        )
    now = utcnow_iso()
    added = 0
    with db.tx() as conn:
        for quote in quotes:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO evidence(event_id, raw_item_id, claim_key, quote, source_url, source_name, "
                    "published_at, fetched_at, created_at) VALUES (?, ?, 'what', ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        raw_item_id,
                        quote,
                        row["url"],
                        row["source_name"],
                        row["published_at"],
                        row["fetched_at"],
                        now,
                    ),
                )
                added += 1
            except sqlite3.IntegrityError:
                pass
    return added


def _pick_direct_quotes(content: str, limit: int = TARGETED_EVIDENCE_LIMIT) -> list[str]:
    """Body sentences for an exact owner-supplied URL, never labels/summary.

    A direct URL proves which page the owner pointed at, so lexical topic
    matching is unnecessary.  It does not prove that the page contains a
    usable article: headings, navigation and metadata are still rejected.
    """
    text = strip_html(normalize_ws(content or ""))
    if not text:
        return []
    quotes: list[str] = []
    for sentence in SENTENCE_SPLIT.split(text):
        snippet = strip_html(normalize_ws(sentence))
        if len(snippet) < 12 or _is_targeted_label_or_metadata(snippet):
            continue
        if len(snippet) > 260:
            snippet = truncate(snippet, 240, "…")
        if snippet and snippet not in quotes:
            quotes.append(snippet)
        if len(quotes) >= limit:
            break
    return quotes


def _pick_quotes(
    title: str,
    summary: str,
    content: str,
    limit: int = 2,
    *,
    topic_hint: str | None = None,
) -> list[str]:
    # 普通自动采集保持原有优先级：摘要 -> 正文 -> 标题。
    # 定向选题走单独路径：只从正文中选与主题相关、有实质信息的段落。
    if topic_hint is not None:
        return _pick_targeted_quotes(content, topic_hint, limit)
    quotes: list[str] = []
    for source_text in (summary, content, title):
        for sentence in SENTENCE_SPLIT.split(source_text or ""):
            text = normalize_ws(sentence)
            text = strip_html(text)
            if len(text) < 12:
                continue
            if len(text) > 260:
                text = truncate(text, 240, "…")
            if text and text not in quotes:
                quotes.append(text)
            if len(quotes) >= limit:
                return quotes
    return quotes


def _topic_terms(text: str) -> list[str]:
    """Bounded lexical terms used to rank targeted body passages."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.strip("._-")
        if len(term) < 2 or term in seen:
            return
        if term.isascii() and term in _TOPIC_ASCII_STOP:
            return
        seen.add(term)
        terms.append(term)

    for raw in re.findall(r"[a-z][a-z0-9.+#/_-]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]+", normalized):
        if raw.isascii():
            add(raw)
            for part in re.split(r"[-_./]+", raw):
                add(part)
            continue
        chunk = raw
        for filler in sorted(_TOPIC_CJK_FILLERS, key=len, reverse=True):
            chunk = chunk.replace(filler, " ")
        for piece in re.findall(r"[\u4e00-\u9fff]+", chunk):
            piece = piece[:24]
            add(piece)
            # Chinese matters are often referred to by a shorter phrase in the
            # body ("智能体记忆机制" -> "记忆"/"机制").  Two-character terms
            # keep the ranking lexical without inventing any new text.
            if len(piece) >= 4:
                for index in range(len(piece) - 1):
                    add(piece[index : index + 2])
        if len(terms) >= 48:
            break
    return terms[:48]


def _topic_term_hit(term: str, haystack: str) -> bool:
    if not term:
        return False
    if term in haystack:
        return True
    # Longer CJK phrases often appear in a shortened form in the body
    # (for example "智能体记忆机制" -> "智能体记忆").  Requiring the first two
    # characters mirrors the existing topic relevance guard and is only one
    # ranking signal, never the sole acceptance rule.
    if len(term) >= 4 and not term.isascii() and term[:2] in haystack:
        return True
    return False


def _is_targeted_label_or_metadata(text: str) -> bool:
    if not text:
        return True
    if _TARGETED_HEADING_RE.match(text):
        return True
    if _TARGETED_METADATA_RE.match(text):
        return True
    if _TARGETED_DATE_RE.match(text):
        return True
    if _TARGETED_READING_RE.match(text):
        return True
    if re.fullmatch(r"(?:https?://|www\.)\S+", text):
        return True
    if re.fullmatch(r"[-*_=~`]{3,}", text):
        return True
    if text.startswith("|") and text.endswith("|"):
        return True
    if len(text) < 24 and text.endswith(("：", ":")):
        return True
    return False


def _pick_targeted_quotes(content: str, topic_hint: str, limit: int) -> list[str]:
    """Select several substantive, topic-overlapping body passages.

    Headings, metadata labels and very short labels are discarded.  Preference
    is given to passages that contain topic terms and concrete detail, with a
    per-segment diversity cap so the writer sees material from later sections
    rather than only the article's opening.  The emitted strings are exact
    normalized substrings of the stored body; no summary is fabricated.
    """
    text = strip_html(normalize_ws(content or ""))
    terms = _topic_terms(topic_hint)
    if not text or not terms:
        return []
    candidates: list[dict[str, Any]] = []
    sentences = SENTENCE_SPLIT.split(text)
    total_sentences = max(1, len(sentences))
    for pos, sentence in enumerate(sentences):
        snippet = strip_html(normalize_ws(sentence))
        if len(snippet) < _TARGETED_MIN_CHARS:
            continue
        if _is_targeted_label_or_metadata(snippet):
            continue
        lowered = snippet.lower()
        hits = sum(1 for term in terms if _topic_term_hit(term, lowered))
        if hits <= 0:
            continue
        score = float(hits * 4)
        if _TARGETED_DETAIL_RE.search(snippet):
            score += 3.0
        if re.search(r"\d", snippet):
            score += 1.0
        if len(snippet) >= 36:
            score += 1.0
        if len(snippet) >= 60:
            score += 1.0
        if snippet.startswith(("“", '"', "「", "『", ">")):
            score -= 2.0
        if snippet.endswith(("：", ":")):
            score -= 3.0
        candidates.append({"pos": pos, "text": snippet, "score": score})
    if not candidates:
        return []

    limit = max(1, min(int(limit), TARGETED_EVIDENCE_LIMIT))
    max_score = max(float(item["score"]) for item in candidates)
    # Do not pad the payload with a weak literary opener just because it
    # mentions the topic once.  The best substantive passage sets the floor.
    min_score = max(4.0, max_score * 0.25)
    ranked = [
        item
        for item in sorted(candidates, key=lambda value: (-value["score"], value["pos"]))
        if float(item["score"]) >= min_score
    ]
    if not ranked:
        return []
    segments = min(3, max(1, total_sentences))
    per_segment_cap = max(2, limit // segments) if segments > 1 else limit

    def segment_for(pos: int) -> int:
        return min(segments - 1, int(pos * segments / total_sentences))

    selected: list[dict[str, Any]] = []
    counts: dict[int, int] = {}
    for item in ranked:
        segment = segment_for(int(item["pos"]))
        if counts.get(segment, 0) >= per_segment_cap:
            continue
        selected.append(item)
        counts[segment] = counts.get(segment, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        chosen = {int(item["pos"]) for item in selected}
        for item in ranked:
            if len(selected) >= limit:
                break
            if int(item["pos"]) in chosen:
                continue
            selected.append(item)
            chosen.add(int(item["pos"]))
    selected.sort(key=lambda item: int(item["pos"]))

    quotes: list[str] = []
    for item in selected:
        snippet = str(item["text"])
        if len(snippet) > _TARGETED_MAX_CHARS:
            snippet = truncate(snippet, _TARGETED_TRUNCATE_CHARS, "…")
        if snippet and snippet not in quotes:
            quotes.append(snippet)
    return quotes[:limit]


def refresh_targeted_evidence(
    db: Database,
    config: Config,
    event_id: int,
    clock: Clock,
    *,
    topic_hint: str | None = None,
    direct_url: str | None = None,
) -> int:
    """Re-extract a request-owned event's evidence when no draft binds it.

    The first attempt may have been made by older extraction logic.  Rebuilding
    gives the retry the richer passages without changing approved/published
    provenance: if any draft references this event, the function is a no-op.
    """
    with db.read() as conn:
        bound = conn.execute(
            "SELECT 1 FROM drafts WHERE event_id=? LIMIT 1", (int(event_id),)
        ).fetchone()
        if bound is not None:
            return 0
        evidence_rows = conn.execute(
            "SELECT DISTINCT raw_item_id FROM evidence WHERE event_id=? ORDER BY raw_item_id",
            (int(event_id),),
        ).fetchall()
        raw_rows = conn.execute(
            "SELECT id, duplicate_of, url FROM raw_items WHERE event_id=? ORDER BY id",
            (int(event_id),),
        ).fetchall()
    raw_ids: list[int] = [int(row["raw_item_id"]) for row in evidence_rows]
    direct_canonical = canonicalize_url(direct_url) if direct_url else ""
    direct_ids: set[int] = set()
    for row in raw_rows:
        raw_id = int(row["id"])
        if direct_canonical and canonicalize_url(str(row["url"] or "")) == direct_canonical:
            direct_ids.add(raw_id)
        if row["duplicate_of"] is not None:
            root_id = _root_raw_item_id(db, raw_id)
            if root_id is not None:
                raw_id = root_id
        if raw_id not in raw_ids:
            raw_ids.append(raw_id)
    if not raw_ids:
        return 0
    with db.tx() as conn:
        conn.execute("DELETE FROM evidence WHERE event_id=?", (int(event_id),))
    added = 0
    for raw_id in raw_ids:
        added += _evidence_for_item(
            db,
            config,
            int(event_id),
            raw_id,
            clock,
            targeted_hint=topic_hint or "",
            direct_content=raw_id in direct_ids,
        )
    return added


# ---------------------------------------------------------------------------
# Research / evidence payload
# ---------------------------------------------------------------------------

def research_event(db: Database, event_id: int, clock: Clock) -> list[dict[str, Any]]:
    # 取出一个事件的全部证据，供打分和写稿使用。
    # 排序：trust_tier 越小越可信（1=厂商官方/一手来源），先列可信来源。
    # 去重键是 (URL, 句子前 80 字)，防止同一来源的近似重复。
    # 最多返回 8 条，够写一篇短文，也不会让提示词无限膨胀。
    with db.read() as conn:
        event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            return []
        rows = conn.execute(
            "SELECT ev.id, ev.quote, ev.source_url, ev.source_name, ev.published_at, ev.fetched_at, "
            "ev.source_role, ev.source_verified, "
            "raw.title AS source_title, raw.published_precision, s.trust_tier "
            "FROM evidence ev JOIN raw_items raw ON raw.id=ev.raw_item_id "
            "JOIN sources s ON s.id=raw.source_id "
            "WHERE ev.event_id=? AND raw.duplicate_of IS NULL ORDER BY s.trust_tier ASC, ev.id ASC",
            (event_id,),
        ).fetchall()
    if not rows:
        return []
    seen: set[tuple[str, str]] = set()
    evidence: list[dict[str, Any]] = []
    for row in rows:
        key = (row["source_url"], row["quote"][:80])
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            {
                "id": str(row["id"]),
                "quote": row["quote"],
                "url": row["source_url"],
                "source_name": row["source_name"],
                "published_at": row["published_at"],
                "date_precision": row["published_precision"] or "unknown",
                "fetched_at": row["fetched_at"],
                "source_title": row["source_title"],
                "trust_tier": int(row["trust_tier"]),
                "source_role": row["source_role"] or "unknown",
                "source_verified": bool(row["source_verified"]),
                "vendor_claim": int(row["trust_tier"]) == 1,
            }
        )
        if len(evidence) >= 8:
            break
    return evidence


def latest_known_published(evidence: list[dict[str, Any]]) -> datetime | None:
    # 取所有证据里最新的发布时间；一个都没有就返回 None。
    values = []
    for item in evidence:
        parsed = _parse_iso(item.get("published_at"))
        if parsed is not None:
            values.append(parsed)
    return max(values) if values else None


def date_is_unknown(evidence: list[dict[str, Any]]) -> bool:
    # “不知道发布时间”是一个重要信号：写稿时必须在限制章节说明，
    # 避免把旧闻包装成今日新闻。
    return latest_known_published(evidence) is None


def score_event(
    db: Database, config: Config, event: sqlite3.Row, evidence: list[dict[str, Any]], clock: Clock
) -> tuple[float, dict[str, Any]]:
    # 打分公式 = 六个正向指标加权 - 两类惩罚，最后夹在 0~1。
    #   相关性 relevance        标题/证据命中受众关键词的比例
    #   实用性 utility          命中“怎么用/教程/价格”等实用词的比例
    #   新鲜度 freshness        越新越高；时间未知只给 0.15
    #   来源多样性 diversity    不同域名越多越高；有官方来源额外加分
    #   新颖度 novelty          与最近 30 篇草稿标题的差异度
    #   可写性 producibility    证据总字数够不够；少于 2 条证据打 6 折
    #   惩罚：标题党词、重复转载比例
    # detail 里保留每个分项，方便用户理解“为什么选它/不选它”。
    now = clock.now()
    audience = config.section("audience")
    keywords = [str(k).lower() for k in audience.get("keywords", [])]
    utility_keywords = [str(k).lower() for k in audience.get("utility_keywords", [])]
    text = (event["title"] + " " + (event["summary"] or "")).lower()
    quote_text = " ".join(str(e["quote"]) for e in evidence).lower()

    rel_hits = sum(1 for k in keywords if k in text or k in quote_text)
    util_hits = sum(1 for k in utility_keywords if k in text or k in quote_text)
    relevance = min(1.0, rel_hits / 3.0)
    utility = min(1.0, util_hits / 2.5)

    latest = latest_known_published(evidence)
    if latest is None:
        freshness = 0.15
        age_hours = None
    else:
        age_hours = max(0.0, (now - latest).total_seconds() / 3600.0)
        freshness = max(0.0, 1.0 - age_hours / float(config.get("limits.max_age_hours", 72)))

    domains = {urlsplit(e["url"]).netloc.lower() for e in evidence}
    source_diversity = min(1.0, len(domains) / 3.0)
    official = any(e["trust_tier"] == 1 for e in evidence)
    if official:
        source_diversity = min(1.0, source_diversity + 0.15)

    recent_titles = [
        row["title"]
        for row in db.fetchall(
            "SELECT title FROM drafts WHERE created_at >= ? ORDER BY id DESC LIMIT 30",
            (to_iso(now - timedelta(days=14)),),
        )
    ]
    max_sim = max((title_similarity(event["title"], t) for t in recent_titles), default=0.0)
    novelty = max(0.0, 1.0 - max_sim)

    evidence_chars = sum(len(e["quote"]) for e in evidence)
    producibility = min(1.0, evidence_chars / 500.0) * (1.0 if len(evidence) >= 2 else 0.6)

    clickbait = sum(1 for word in CLICKBAIT if word in event["title"])
    penalty = min(0.3, clickbait * 0.1)
    # Many identical syndicated copies are not stronger evidence.
    # 十家网站转载同一篇通稿，不代表“十个来源证实”。这里按规范 URL
    # 统计重复比例，重复越多扣分越多。
    urls = [e["url"] for e in evidence]
    duplicate_ratio = 0.0
    if len(urls) > 1:
        canonical_count = len({canonicalize_url(u) for u in urls})
        duplicate_ratio = 1.0 - canonical_count / len(urls)
    penalty += duplicate_ratio * 0.1

    score = (
        0.27 * relevance
        + 0.16 * utility
        + 0.22 * freshness
        + 0.14 * source_diversity
        + 0.12 * novelty
        + 0.09 * producibility
        - penalty
    )
    score = max(0.0, min(1.0, score))
    detail = {
        "relevance": round(relevance, 3),
        "utility": round(utility, 3),
        "freshness": round(freshness, 3),
        "source_diversity": round(source_diversity, 3),
        "novelty": round(novelty, 3),
        "producibility": round(producibility, 3),
        "penalty": round(penalty, 3),
        "age_hours": None if age_hours is None else round(age_hours, 1),
        "domains": sorted(domains),
        "evidence_count": len(evidence),
        "date_unknown": latest is None,
    }
    return round(score, 4), detail


def refresh_scores(db: Database, config: Config, clock: Clock) -> list[dict[str, Any]]:
    # 重新给最近窗口内的活跃事件打分，按分数从高到低返回。
    # 只取最近 100 条，避免事件越积越多时每次打分都很慢。
    rows = db.fetchall(
        "SELECT * FROM events WHERE status='active' AND first_seen_at >= ? ORDER BY id DESC LIMIT 100",
        (to_iso(clock.now() - timedelta(days=int(config.get("limits.event_window_days", 7)))),),
    )
    scored = []
    for event in rows:
        evidence = research_event(db, int(event["id"]), clock)
        if not evidence:
            continue
        score, detail = score_event(db, config, event, evidence, clock)
        with db.tx() as conn:
            conn.execute(
                "UPDATE events SET score=?, score_detail=? WHERE id=?",
                (score, json_dumps(detail), event["id"]),
            )
        scored.append({"event_id": event["id"], "title": event["title"], "score": score, "detail": detail})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def ensure_research_queue(db: Database, config: Config, clock: Clock) -> list[dict[str, Any]]:
    """Score active events and promote at most max_queued_topics to researched."""
    # 队列上限：已经排队的达到 max_queued_topics 就不再提升新事件，
    # 避免一次堆几十个选题。只有分数过线、且没超过最大年龄的才提升。
    max_queue = int(config.get("limits.max_queued_topics", 3))
    with db.read() as conn:
        queued = conn.execute("SELECT COUNT(*) AS n FROM events WHERE status='researched'").fetchone()["n"]
    if queued >= max_queue:
        return []
    candidates = refresh_scores(db, config, clock)
    min_score = float(config.get("scoring.min_score", 0.45))
    selected = []
    with db.tx() as conn:
        for item in candidates:
            if len(selected) + int(queued) >= max_queue:
                break
            if item["score"] < min_score:
                continue
            if item["detail"].get("age_hours") is not None and item["detail"]["age_hours"] > float(
                config.get("limits.max_age_hours", 72)
            ):
                continue
            conn.execute("UPDATE events SET status='researched' WHERE id=? AND status='active'", (item["event_id"],))
            selected.append(item)
    return selected


def next_researched_event(db: Database) -> sqlite3.Row | None:
    # 取当前分数最高的待写事件；分数相同则先来先写。
    return db.fetchone(
        "SELECT * FROM events WHERE status='researched' AND substr(event_key,1,6) <> 'topic_' ORDER BY score DESC, id ASC LIMIT 1"
    )


def evidence_by_id(db: Database, event_id: int) -> dict[str, dict[str, Any]]:
    # 返回 {证据ID: 证据字典}，写稿/校验时用 ID 反查原文。
    # 注意这里不像 research_event 那样去重或截断，因为校验需要完整集合。
    with db.read() as conn:
        rows = conn.execute(
            "SELECT ev.id, ev.quote, ev.source_url, ev.source_name, ev.published_at, ev.fetched_at, "
            "ev.source_role, ev.source_verified, "
            "raw.title AS source_title, raw.published_precision, s.trust_tier "
            "FROM evidence ev JOIN raw_items raw ON raw.id=ev.raw_item_id "
            "JOIN sources s ON s.id=raw.source_id WHERE ev.event_id=?",
            (event_id,),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[str(row["id"])] = {
            "id": str(row["id"]),
            "quote": row["quote"],
            "url": row["source_url"],
            "source_name": row["source_name"],
            "published_at": row["published_at"],
            "date_precision": row["published_precision"] or "unknown",
            "fetched_at": row["fetched_at"],
            "source_title": row["source_title"],
            "trust_tier": int(row["trust_tier"]),
            "source_role": row["source_role"] or "unknown",
            "source_verified": bool(row["source_verified"]),
            "vendor_claim": int(row["trust_tier"]) == 1,
        }
    return out


def account_memory(db: Database, config: Config) -> dict[str, Any]:
    # 账号记忆：从历史审批/修改里积累的偏好。
    # 四类：禁用表达、修改理由、已认可的写法、来源质量评价。
    # recent_topics 单独从最近 20 篇草稿取，用来避免连续写重复话题。
    account_id = str(config.get("account.id", "xhs-main"))
    rows = db.fetchall(
        "SELECT kind, key, value, weight FROM account_memory WHERE account_id=? ORDER BY updated_at DESC",
        (account_id,),
    )
    memory: dict[str, list[str]] = {
        "banned_expressions": [],
        "edit_reasons": [],
        "approved_style": [],
        "source_quality": [],
    }
    for row in rows:
        value = str(row["value"])
        if row["kind"] == "banned_expression":
            memory["banned_expressions"].append(value)
        elif row["kind"] == "edit_reason":
            memory["edit_reasons"].append(value)
        elif row["kind"] == "approved_style":
            memory["approved_style"].append(value)
        elif row["kind"] == "source_quality":
            memory["source_quality"].append(value)
    memory["recent_topics"] = [
        row["title"]
        for row in db.fetchall(
            "SELECT title FROM drafts ORDER BY id DESC LIMIT 20"
        )
    ]
    return memory


def build_writer_payload(
    db: Database,
    config: Config,
    event: sqlite3.Row,
    evidence: list[dict[str, Any]],
    *,
    clock: Clock | None = None,
    instruction: str | None = None,
    current_draft: dict | None = None,
    revision_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # 组装给写稿模型的输入：
    #   event      事件标题/摘要/是否日期未知
    #   evidence   最多 6 条证据（含来源、可信层级、是否厂商说法）
    #   audience   受众描述和关键词
    #   constraints 标题/正文长度、禁用词
    #   editorial_profile 长期、可编辑的账号偏好（不是模板）
    #   memory     账号历史偏好
    #   now/timezone 让模型知道“今天”是哪天
    # 注意：网页原文只作为 evidence 数据传入，不会被当成指令执行。
    from ..editorial import load_editorial_profile

    memory = account_memory(db, config)
    profile = load_editorial_profile(config)
    date_unknown = date_is_unknown(evidence)
    payload: dict[str, Any] = {
        "event": {
            "id": event["id"],
            "title": event["title"],
            "summary": event["summary"],
            "latest_published_at": event["latest_published_at"],
            "date_unknown": date_unknown,
        },
        "evidence": [
            {
                "id": e["id"],
                "quote": e["quote"],
                "source_name": e["source_name"],
                "source_url": e["url"],
                "published_at": e["published_at"],
                "trust_tier": e["trust_tier"],
                "source_role": e.get("source_role", "unknown"),
                "source_verified": bool(e.get("source_verified")),
                "vendor_claim": e["vendor_claim"],
            }
            for e in evidence[:6]
        ],
        "audience": {
            "description": config.get("audience.description"),
            "keywords": config.get("audience.keywords", []),
        },
        "constraints": {
            "title_max_chars": int(config.get("writer.title_max_chars", 20)),
            "body_max_chars": int(config.get("writer.body_max_chars", 900)),
            "storyboard_max_cards": int(config.get("writer.storyboard_max_cards", 4)),
            "storyboard_max_chars": int(config.get("writer.storyboard_max_chars", 900)),
            "render_max_cards": int(config.get("render.max_cards", 6)),
            "banned_phrases": config.get("writer.banned_phrases", []),
            "visual_layouts": [
                "cover",
                "comparison",
                "process",
                "keypoints",
                "diagram",
                "argument",
                "annotated",
            ],
            "caption_policy": (
                "no universal caption/image length ratio and no required card count; "
                "choose image_led, caption_led or balanced for this topic and justify it"
            ),
        },
        "editorial_profile": profile.as_prompt_dict(),
        "memory": memory,
        "timezone": config.timezone_name,
        "now": to_iso((clock or SystemClock()).now()),
    }
    if instruction is not None:
        # Editorial instructions are deliberately separate from ``evidence``.
        # They may shape angle/audience but are not a factual source and cannot
        # override the workflow or content rules.
        payload["instruction"] = instruction
        payload["editorial_instructions"] = instruction
    if current_draft is not None:
        payload["current_draft"] = current_draft
    if revision_request is not None:
        payload["revision_contract"] = revision_request
        payload["revision_note"] = (
            "Read current_draft.caption, current_draft.editorial_plan and "
            "current_draft.storyboard together with the raw instruction. Choose one coherent "
            "strategy for this revision; do not merely lengthen the old body, do not force four "
            "headings, and do not repeat caption paragraphs on image cards."
        )
    return payload


def clock_now(db: Database) -> datetime:
    # 简单的当前本地时间（带时区），供少数同步调用场景使用。
    return datetime.now().astimezone()


def _mark_source_failure(db: Database, source: sqlite3.Row, exc: Exception, clock: Clock) -> None:
    # 来源失败后的退避：120 秒、240 秒、480 秒……最多 30 分钟。
    # fail_count 会一直累加，用来判断这个源是不是长期坏了。
    fail_count = int(source["fail_count"]) + 1
    base = min(1800.0, 120.0 * (2 ** min(fail_count, 5)))
    from ..timeutil import utcnow_iso as _now

    now = clock.now()
    with db.tx() as conn:
        conn.execute(
            "UPDATE sources SET last_fetch_at=?, last_status='error', last_error=?, fail_count=?, "
            "next_fetch_at=? WHERE id=?",
            (
                to_iso(now),
                str(exc)[:500],
                fail_count,
                to_iso(now + timedelta(seconds=base)),
                source["id"],
            ),
        )
        audit(conn, "collector", "source.failed", entity="source", entity_id=source["id"], detail={"error": str(exc)[:300]})


def _next_fetch(config: Config, clock: Clock, failures: int) -> datetime:
    # 正常抓取间隔。failures 参数目前未参与计算（失败退避在
    # _mark_source_failure 里单独处理）。
    base = float(config.get("limits.poll_interval_seconds", 1800))
    return clock.now() + timedelta(seconds=base)


def _max_iso(a: str | None, b: str | None) -> str | None:
    # 两个 ISO 时间字符串取较晚的一个；空值会被忽略。
    values = [v for v in (a, b) if v]
    return max(values) if values else None


def _min_iso(a: str | None, b: str | None) -> str | None:
    # 两个 ISO 时间字符串取较早的一个；空值会被忽略。
    values = [v for v in (a, b) if v]
    return min(values) if values else None


def _parse_iso(value: str | None):
    # 局部导入 parse_iso，避免模块顶部出现循环依赖。
    from ..timeutil import parse_iso

    return parse_iso(value)


