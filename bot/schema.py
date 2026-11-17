"""SQLite schema migrations.

The database is the source of truth for the whole operating loop: sources,
events, evidence, drafts, approvals, publish attempts, jobs and audit trail.
"""

from __future__ import annotations

# =============================================================================
# 中文导读：这个文件定义整个系统的“账本结构”
# =============================================================================
# 数据库里存的不只是内容，更是状态。整个运营闭环靠下面这些表串起来：
#
#   sources           配置了哪些来源（RSS / 本地 fixture），上次抓取情况
#   raw_items         抓到的每一篇文章（含去重指纹、原始 URL）
#   events            把多篇讲同一件事的文章聚成一个“事件/选题”
#   evidence          从文章里摘出的证据句子（写稿只能引用这些）
#   drafts            草稿的每个版本（正文、哈希、计划发布时间、状态）
#   draft_images      草稿对应的 PNG 卡片（路径 + 内容哈希）
#   approvals         审批单：绑定某一版正文/图片/账号/时间
#   publish_attempts  每次发布尝试（唯一键保证同一审批最多一次）
#   jobs              持久化任务队列（采集/写稿/发布/选题），带租约和重试
#   topic_requests    手机飞书“选题/写一篇”委托的持久化状态与产物关联
#   notifications     待发送的通知（发飞书或写本地文件）
#   inbound_messages  收到的审批命令（按消息 ID 去重，防重复点击）
#   llm_usage         每次模型调用的 token 与费用（预算账本）
#   account_memory    从历史审批里学到的账号口味（禁用表达、修改理由等）
#   metrics_snapshots 发布后 24h/72h 的数据快照（拿不到就记 missing）
#   audit_log         只追加的审计流水（谁做了什么）
#
# 几个贯穿全项目的状态词：
#   discovered -> researched -> drafted -> review_pending -> approved
#              -> publishing -> published
#   旁支状态：rejected（拒绝）、expired（过期）、failed（失败）、
#             publish_unknown（发布结果未知，必须人工核对）、paused（暂停）
#
# 为什么要用数据库唯一约束？
#   并发点击“批准”、网络回调重试、进程重启都可能让同一件事执行两次。
#   代码里的 if 判断在并发下并不可靠；数据库的唯一索引才是最后一道闸门。
#   例如 ux_approvals_one_pending 保证一个草稿最多只有一张待审批单，
#   publish_attempts.dedupe_key 保证同一审批最多产生一次发布尝试。
# =============================================================================

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        r"""
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'rss',
    trust_tier INTEGER NOT NULL DEFAULT 2,
    enabled INTEGER NOT NULL DEFAULT 1,
    etag TEXT,
    last_modified TEXT,
    last_fetch_at TEXT,
    last_status TEXT,
    last_error TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    next_fetch_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    latest_published_at TEXT,
    earliest_published_at TEXT,
    score REAL NOT NULL DEFAULT 0,
    score_detail TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    reject_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_events_status_score ON events(status, score DESC);

CREATE TABLE raw_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    content TEXT,
    author TEXT,
    published_at TEXT,
    published_precision TEXT NOT NULL DEFAULT 'unknown',
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    title_key TEXT NOT NULL,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    duplicate_of INTEGER REFERENCES raw_items(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_raw_items_source_url ON raw_items(source_id, canonical_url);
CREATE INDEX ix_raw_items_hash ON raw_items(content_hash);
CREATE INDEX ix_raw_items_event ON raw_items(event_id);

CREATE TABLE evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    raw_item_id INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
    claim_key TEXT,
    quote TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_name TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(event_id, raw_item_id, quote)
);
CREATE INDEX ix_evidence_event ON evidence(event_id);

CREATE TABLE drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    body_json TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    scheduled_at TEXT,
    expires_at TEXT,
    images_sha256 TEXT,
    revision_feedback TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, version)
);
CREATE INDEX ix_drafts_status ON drafts(status);
CREATE INDEX ix_drafts_event ON drafts(event_id);

CREATE TABLE draft_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'card',
    created_at TEXT NOT NULL,
    UNIQUE(draft_id, ordinal)
);

CREATE TABLE approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    draft_version INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    images_sha256 TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    scheduled_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    channel TEXT,
    created_at TEXT NOT NULL,
    notified_at TEXT,
    decided_at TEXT,
    decided_by TEXT,
    decision_comment TEXT
);
CREATE INDEX ix_approvals_status ON approvals(status, expires_at);
CREATE UNIQUE INDEX ux_approvals_one_pending ON approvals(draft_id) WHERE status = 'pending';

CREATE TABLE publish_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id INTEGER NOT NULL REFERENCES approvals(id),
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    dedupe_key TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    external_post_id TEXT,
    external_url TEXT,
    receipt_json TEXT,
    error TEXT,
    reconcile_note TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_publish_attempts_state ON publish_attempts(state);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_jobs_due ON jobs(state, next_run_at);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE inbound_messages (
    message_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    sender_id TEXT,
    chat_id TEXT,
    chat_type TEXT,
    text TEXT,
    received_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    ref TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    usage_known INTEGER NOT NULL DEFAULT 1,
    cost_usd REAL NOT NULL DEFAULT 0,
    reserved_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT
);
CREATE INDEX ix_llm_usage_at ON llm_usage(at);

CREATE TABLE account_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, kind, key)
);

CREATE TABLE metrics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    checkpoint TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    missing_json TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(draft_id, checkpoint)
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    detail_json TEXT
);
CREATE INDEX ix_audit_at ON audit_log(at);
""",
    ),
    (
        2,
        r"""
-- A model call is reserved before network I/O and settled afterwards.
-- settled=0 means the estimate is still held; settled=1 with a non-zero
-- reserved_usd means the provider outcome/usage was unknown and the
-- conservative reservation is retained permanently.
ALTER TABLE llm_usage ADD COLUMN settled INTEGER NOT NULL DEFAULT 1;
CREATE INDEX ix_llm_usage_settled ON llm_usage(settled);
""",
    ),
    (
        3,
        r"""
-- A user-commissioned topic request.  The inbound message_id is unique, so a
-- replay or concurrent Feishu callback cannot create a second request, job or
-- acknowledgement.  The request row is the durable progress record across
-- worker restarts; event_id/evidence_ids_json/draft_id/approval_id preserve
-- exactly which research and artifact were produced for this commission.
CREATE TABLE topic_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    message_id TEXT NOT NULL UNIQUE,
    channel TEXT,
    sender_id TEXT NOT NULL,
    chat_id TEXT,
    chat_type TEXT,
    raw_text TEXT NOT NULL,
    command TEXT NOT NULL,
    topic TEXT NOT NULL,
    instruction TEXT NOT NULL,
    supplied_url TEXT,
    url_note TEXT,
    search_query TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    progress TEXT,
    day_key TEXT NOT NULL,
    event_id INTEGER,
    draft_id INTEGER,
    approval_id INTEGER,
    evidence_ids_json TEXT,
    result_code TEXT,
    error TEXT,
    job_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX ix_topic_requests_status ON topic_requests(status, created_at);
CREATE INDEX ix_topic_requests_day ON topic_requests(day_key);
CREATE INDEX ix_topic_requests_message ON topic_requests(message_id);

-- A commissioned draft is linked in the same transaction that inserts it, so
-- a crash after draft creation is recoverable without a second model call or a
-- duplicate draft.  The partial unique index enforces one draft per request.
ALTER TABLE drafts ADD COLUMN topic_request_id INTEGER;
CREATE UNIQUE INDEX ux_drafts_topic_request
    ON drafts(topic_request_id) WHERE topic_request_id IS NOT NULL;
""",
    ),
    (
        4,
        r"""
-- Model-assisted source review is scoped to one commissioned topic.  The
-- suggested hosts are audited here but are never added to the persistent
-- source allowlist; the owner still approves the final draft.
ALTER TABLE topic_requests ADD COLUMN suggested_domains_json TEXT;
ALTER TABLE topic_requests ADD COLUMN source_review_json TEXT;
""",
    ),
    (
        5,
        r"""
-- A durable, queryable business result for every approval outcome.  This is
-- deliberately separate from jobs (a job may be consumed while the business
-- result is blocked/unknown) and from publish_attempts (pre-submit blocks have
-- no attempt).  One attempt has one result row; manual/read-only reconciliation
-- updates that row instead of inventing a second submission.
CREATE TABLE publish_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id INTEGER NOT NULL REFERENCES approvals(id) ON DELETE CASCADE,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    attempt_id INTEGER REFERENCES publish_attempts(id) ON DELETE SET NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    stage TEXT NOT NULL,
    state TEXT NOT NULL,
    reason_code TEXT,
    detail TEXT,
    external_post_id TEXT,
    external_url TEXT,
    receipt_json TEXT,
    notification_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_publish_results_approval ON publish_results(approval_id);
CREATE INDEX ix_publish_results_state ON publish_results(state);
CREATE INDEX ix_publish_results_created ON publish_results(created_at);

-- Read-only reconciliation is rate/time bounded and append-only.  A missing or
-- ambiguous lookup is recorded as unknown; it never proves non-publication and
-- never releases a daily quota slot.
CREATE TABLE publish_reconcile_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES publish_attempts(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT,
    detail TEXT,
    evidence_json TEXT
);
CREATE INDEX ix_publish_reconcile_attempt ON publish_reconcile_checks(attempt_id, checked_at);
""",
    ),
    (
        6,
        r"""
-- S1: append-only research diagnostics.  These records explain why a topic
-- did or did not produce evidence; they deliberately do NOT store full article
-- bodies or raw provider responses.  topic_requests keeps only the short
-- lifecycle fields inherited from earlier migrations.
CREATE TABLE research_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_request_id INTEGER REFERENCES topic_requests(id) ON DELETE SET NULL,
    query TEXT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    counts_json TEXT,
    diagnostics_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_research_attempts_request
    ON research_attempts(topic_request_id, started_at);
CREATE INDEX ix_research_attempts_status
    ON research_attempts(status, created_at);

CREATE TABLE research_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES research_attempts(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    title TEXT,
    url TEXT,
    canonical_url TEXT,
    domain TEXT,
    domain_verified INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL,
    decision_code TEXT,
    fetch_status INTEGER,
    body_length INTEGER,
    body_sha256 TEXT,
    latency_ms INTEGER,
    detail_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_research_candidates_attempt
    ON research_candidates(attempt_id, ordinal);
CREATE INDEX ix_research_candidates_code
    ON research_candidates(decision_code);
""",
    ),
    (
        7,
        r"""
-- S1 review repair: keep a one-way correlation fingerprint for the exact
-- (unsanitized) URL and separate "did we verify this source identity" from
-- "did an admin trust this body".  Diagnostics themselves never store query
-- tokens or credentials.
ALTER TABLE research_candidates ADD COLUMN url_fingerprint TEXT;
ALTER TABLE research_candidates ADD COLUMN citable_as_official INTEGER NOT NULL DEFAULT 0;
ALTER TABLE research_candidates ADD COLUMN date_status TEXT;
""",
    ),
    (
        8,
        r"""
-- S1 closeout / S2 / S3.  Research-mode is explicit and survives through the
-- evidence, draft and approval chain.  Source identity is separate from
-- discovery: a candidate only becomes a verified role after the batch judge
-- sees actual material and a verifiable relation.
ALTER TABLE topic_requests ADD COLUMN research_mode TEXT NOT NULL DEFAULT 'news';
ALTER TABLE topic_requests ADD COLUMN research_plan_json TEXT;
ALTER TABLE drafts ADD COLUMN research_mode TEXT NOT NULL DEFAULT 'news';
ALTER TABLE evidence ADD COLUMN source_role TEXT;
ALTER TABLE evidence ADD COLUMN source_verified INTEGER NOT NULL DEFAULT 0;

CREATE TABLE research_cache (
    cache_key TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    query TEXT,
    research_mode TEXT NOT NULL DEFAULT 'news',
    policy_key TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    counts_json TEXT,
    diagnostics_json TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX ix_research_cache_expires ON research_cache(expires_at);

CREATE TABLE source_identity_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    organization TEXT,
    role TEXT NOT NULL,
    relation TEXT,
    material_fingerprint TEXT NOT NULL,
    verdict_json TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(domain, material_fingerprint)
);
CREATE INDEX ix_source_identity_expires ON source_identity_cache(expires_at);
""",
    ),
    (
        9,
        r"""
-- Creator Agent V2: durable model-driven run/step/artifact journal.  Existing
-- drafts/approvals remain the publishing contract; these tables add loop state
-- without rewriting history.
CREATE TABLE creator_agent_runs (
    run_key TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    brief_json TEXT NOT NULL,
    counters_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT,
    policy_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_creator_agent_runs_status
    ON creator_agent_runs(status, updated_at);

CREATE TABLE creator_agent_steps (
    run_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    action TEXT NOT NULL,
    args_json TEXT NOT NULL,
    intent_summary TEXT,
    expected_result TEXT,
    result_json TEXT,
    status TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_key, seq),
    FOREIGN KEY (run_key) REFERENCES creator_agent_runs(run_key)
);
CREATE INDEX ix_creator_agent_steps_run
    ON creator_agent_steps(run_key, seq);

CREATE TABLE creator_agent_artifacts (
    run_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    draft_id INTEGER,
    content_json TEXT NOT NULL,
    images_json TEXT NOT NULL DEFAULT '[]',
    claims_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    brief_hash TEXT NOT NULL,
    parent_version INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_key, version),
    FOREIGN KEY (run_key) REFERENCES creator_agent_runs(run_key)
);

CREATE TABLE creator_agent_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    source_run_key TEXT,
    source_message_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_creator_agent_preferences_account
    ON creator_agent_preferences(account_id, active, scope);
""",
    ),
    (
        10,
        r"""
-- Creator Agent V2: durable submission identity and material journal.
ALTER TABLE drafts ADD COLUMN submission_key TEXT;
ALTER TABLE drafts ADD COLUMN submission_hash TEXT;
CREATE UNIQUE INDEX ux_drafts_submission_key
    ON drafts(submission_key) WHERE submission_key IS NOT NULL;
CREATE TABLE creator_agent_materials (
    run_key TEXT NOT NULL,
    material_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    role TEXT,
    published_at TEXT,
    fetched_at TEXT,
    content_type TEXT,
    content_hash TEXT,
    quote TEXT,
    content TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_key, material_id),
    FOREIGN KEY (run_key) REFERENCES creator_agent_runs(run_key)
);
CREATE INDEX ix_creator_agent_materials_run
    ON creator_agent_materials(run_key, material_id);
""",
    ),
]
