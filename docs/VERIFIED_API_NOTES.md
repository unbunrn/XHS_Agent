# Verified API notes for follow-up
Verified by parent agent 2026-11-06 +08:00. These are API facts, not proof of live platform integration.

## XHS bridge
Source: https://github.com/xpzouying/xiaohongshu-mcp/blob/main/docs/API.md
GET /api/v1/user/me documents success=true, data.data.userBasicInfo.{nickname,desc,redId,gender,ipLocation,images,imageb}. There is NO documented stable internal user_id in this response.
redId is the public Xiaohongshu account number, not the internal user ID. Do not invent data.user_id based on this documentation. If comparing redId, configure and label expected_red_id explicitly; if requiring immutable platform ID, stay blocked until a bridge adapter supplies verified identity.
POST /api/v1/publish documents success=true and data {title,content,images,status:"发布完成"} with no note_id or URL. This receipt cannot prove a stable post identity. Keep publish_unknown and require manual reconciliation, never retry automatically.
The parent fetched the upstream documentation successfully through the GitHub connector.

## DeepSeek
Official https://api-docs.deepseek.com/guides/thinking_mode/ says thinking enabled by default at high effort. Explicit request {"thinking":{"type":"disabled"}} is appropriate for short JSON drafts with a bounded output. Do not rely on the default with max_tokens=1800.
Official https://api-docs.deepseek.com/guides/search/:
POST https://api.deepseek.com/v1/tools/search
request {"query":"...","count":3,"need_content":true}
response {"results":[{"title":"...","url":"...","content":"...","summary":"...","date":"RFC3339 or null"}]}.
Use content as evidence, not synthesized summary. Search date may be unknown; no invention.
POST https://api.deepseek.com/v1/tools/fetch request {"url":"https://public-host/path"}, response {"title":"...","content":"Markdown..."}.
Both use existing DeepSeek API key, official api.deepseek.com host, no credential redirects.
Cost: USD .00075 per successful nonempty search or successful fetch. Failed/empty searches aren't billed per official docs.
Parent verified HTTP200 models (deepseek-flash/deepseek-v4-pro), two searches, one Fetch; public source output in var/live-verification (no secret there). Do NOT read config/secrets.toml. Parent will run paid generation.
Local DNS maps some public websites to 198.18.0.0/15 and the RSS SSRF guard correctly blocks them. DO NOT whitelist that range or disable the guard. A fixed-host authenticated official Search/Fetch adapter is a legitimate alternate collection source.

## Feishu
Historical DSH setup note superseded: parent verified the app, real text and two image deliveries, owner private-chat approval, and result receipt on 2026-11-06. See ACCEPTANCE_20261106.md for evidence. No social publication.
https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure- says long-connection callback must finish within 3 seconds. Current synchronous revise_draft inside gateway is too slow.
Minimum permissions for our use: im:message:send_as_bot, im:message.p2p_msg:readonly, im:resource.
Need a self-built app with bot ability; im.message.receive_v1 subscription, long connection running when saving subscription, app availability includes owner, published version. Parent is writing FEISHU_SETUP.md.

