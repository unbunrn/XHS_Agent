# Editorial upgrade result

Status: implemented and verified with focused offline tests. **No live model call, external service, real publishing or live-data verification was performed.**

This continues the partially implemented upgrade and incorporates the owner correction: the account should behave like an editor choosing a format per topic, not a fixed caption/image template or a body-to-image converter.

## What changed

### 1. Independent image storyboard

`DraftContent` now persists:

- `caption`: the publishing text, without a forced four-heading structure;
- `editorial_plan`: audience need, angle, takeaway, caption/image division, rationale, chosen strategy and visual approach;
- `storyboard`: a cover plus zero or more content cards.

Cards are validated and rendered independently from the caption. The renderer never flows the full caption into images for newly generated work. A one-cover storyboard is valid.

Supported local visual vocabulary:

| Layout | Use | Rendering |
| --- | --- | --- |
| `cover` | hook / one-image post | large text treatment |
| `comparison` | two sourced positions side by side | two columns |
| `process` | source explicitly describes a sequence | numbered steps and a connector, no arrowheads |
| `keypoints` | a few take-aways | numbered rows |
| `diagram` | layers / relationships | stacked blocks, deliberately no arrows or implied order |
| `argument` | claim plus reasons | claim/reason rows |
| `annotated` | source excerpt plus notes | quoted excerpt panel and numbered notes |

The `process` validator requires source-supported sequence language (or explicit numbered steps). This prevents turning a memory architecture into a fake pipeline just to fit a template. The renderer uses local fonts and geometric primitives only; it does not generate illustrations, fetch stock art or copy sample images.

### 2. Per-topic editorial judgment

The writer prompt and payload now ask the model to choose `image_led`, `caption_led` or `balanced` per topic and justify it from the actual content and audience. There is no universal caption minimum, image/body length ratio, required card count, fixed card combination or required four-section body.

A durable, nonsecret editorial profile is included on every generation:

- default file: `config/editorial-profile.toml`;
- editable by hand or via `save_editorial_profile(..., explicit_owner_request=True)`;
- treated as expression/division guidance, never evidence;
- per-post instructions and fetched webpage text are never persisted automatically.

The profile explains only a supported preference boundary. It cannot make the system less strict about evidence, approvals, owner authorization, expiry or publishing.

### 3. Revision understanding and acceptance

Revision payloads include the previous caption, editorial plan and storyboard, the raw owner instruction, and a structured revision contract with artifact-level intents. The generating model is told to make a fresh editorial decision, not merely lengthen the old body.

Acceptance is independent of the generating model:

1. deterministic artifact diff rejects unchanged requested artifacts;
2. paragraph-level duplication is detected (shared terms, titles and short conclusions are allowed);
3. directional length checks apply only to the artifact the owner named, and are skipped when the owner offered both directions;
4. layout changes are checked when requested;
5. exactly one bounded semantic review call compares before/after body, plan and storyboard;
6. the review must return `satisfied`, `strategy_consistent`, no remaining paragraph duplication, no invented experience/engagement and no problems;
7. deterministic failures stop before spending on review.

If any step fails, the worker reports **修改未满足**, the old approval stays frozen, and no new version is presented as successful. An accepted revision reports the actual body change, image count, layout/strategy change and paragraph-duplication change in the owner notification.

### 4. Evidence, approval and topic linkage

- Every image card requires `evidence_ids`.
- Numbers on cards and in the caption/title are checked against evidence cited anywhere in the draft.
- Image-only evidence is included in the body source list.
- Rendered image count must equal storyboard card count.
- New approvals are built from the new body hash and newly rendered PNG bytes; old body_json and PNGs are never rewritten.
- Revision supersession, new approval creation, notification queuing and topic-request latest-result linkage happen in one transaction.
- For a commissioned topic, the old draft's `topic_request_id` is cleared and the new draft is linked in the same transaction that updates `topic_requests.draft_id`, `approval_id` and `result_code`.

## Tests

Command:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Result: **164 tests, OK** (approximately 188 seconds, offline fixtures only).

Focused file:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_editorial_autonomy -q
```

Result: **12 tests, OK**.

The focused tests cover:

- all seven local layouts render as PNGs with the expected card count;
- clause-local revision intents (images shorten / caption expand) and either/or instructions keeping both directions;
- distinct content yielding distinct strategies, visual approaches, layouts and page counts;
- image-led short caption and caption-led brief visual both valid, and a one-cover storyboard is valid;
- allowed shared take-away vs rejected copied paragraph;
- old section-only drafts deserialize and render with the legacy path;
- unsupported image number, citation-free image card and unjustified process layout rejected;
- unchanged revision rejected without calling semantic review or notifying success;
- self-reported review success rejected when required strategy confirmation is absent;
- budget exhaustion during review leaves the old artifact frozen;
- owner feedback changes caption/storyboard split, creates a new approval bound to new images, and invalidates tampered new images;
- old approval fingerprint/body/image bytes remain unchanged;
- topic latest-result linkage moves transactionally on revision;
- durable profile is passed to generation while per-post instructions are not persisted.

## Honest limits

- This does **not** prove live model autonomy or live publishing success. The live DeepSeek path was not called; there were no keys, network calls or real accounts.
- `FakeLLM` is a deterministic offline test double that varies plans for test coverage. Its output is not evidence of editorial quality.
- Supported visuals are text and local geometric layouts only. There is no illustration generation, image understanding, typography layout solver or source-image annotation pipeline.
- `diagram` blocks do not encode edges/arrows. `process` relies on bounded sequence language; some legitimate sequences may be rejected and should be represented as `keypoints`/`diagram` instead.
- Duplication detection is paragraph-level and lexical. Paraphrased duplication can pass deterministic checks and is left to the bounded semantic review, which is itself a text model rather than proof.
- Strategy-specific acceptance is intentionally bounded. It does not guarantee that every editorial choice is good; it only rejects unchanged requested artifacts, structural/provider violations, copied paragraphs and reviewer-confirmed mismatches.
- The profile is edited locally as TOML or through the explicit helper. No new chat/console profile-editing command was added.
- No database migration was required: the storyboard and plan live in the existing `drafts.body_json`, and topic linkage uses existing columns.

## Changed files

- `bot/content.py`
- `bot/editorial.py`
- `bot/render.py`
- `bot/adapters/llm.py`
- `bot/services/drafts.py`
- `bot/services/research.py`
- `bot/worker.py`
- `bot/config.py`
- `config/editorial-profile.toml` (new)
- `tests/test_editorial_autonomy.py` (new)
- `tests/test_notify.py` (updated for caption-based body without mandatory four headings)
- `docs/EDITORIAL_UPGRADE_RESULT.md` (this file)
