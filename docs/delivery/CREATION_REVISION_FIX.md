# 创作与修改统一修复（XH-RNAWQM）

状态：代码修复与离线验证完成；2026-11-20 00:09 仅重启 worker 使修订 JSON 修复生效；未提交 Git。全量测试结果见文末。
日期：2026-11-19
范围：统一 Creator 修改入口，继承父材料，局部修改，结构化校验反馈；不重发历史作品，不新增 Agent。

## 最近失败分类

- 19:10 选题（run `ca-d1d67be847b84c1723ea`）：13 步、review_cycles 2/3、model_calls 18/36、invalid_decisions 4/3。模型在 v3 修订后没有按提示 review，连续 4 次 `revise_artifact` 被 `_revise_phase_problem` 拒绝；额度未耗尽，终止原因是连续无效决策超限。属于“模型未遵循阶段约束”，不是材料或发布问题。
- 19:23 `修改 XH-RNAWQM`（job 308）：旧 `drafts.revise_draft` 走 `research_event()` 的 evidence 摘要，未继承父 Creator run 的 M1/M2 全文。原稿引用的 `65,536` 位于 M2 正文第 605 字符，摘要中没有；`content._check_numbers` 按卡片 `evidence_ids` 做字面校验，直接抛 `LLMOutputError`，单次调用失败后没有定向修复或中文回执。属于“双路径 + 材料截断 + 单点硬校验”。

## 修改

- `bot/services/approvals.py`
  - 所有以“修改”开头的命令（含 `修改 XH-...`、`V2 修改`）统一进入 Creator revision；不再落到旧 `revise` job。
  - 修改成功在同一个事务内写 inbound 并入队中文回执；失败按 ambiguous / no_target / in_flight / conflict / needs_instruction 返回明确中文说明。
  - 保留工作区原有未提交的帮助命令。
- `bot/services/creator_agent/entry.py`
  - 修改目标按账号和显式 XH 码筛选；无编号多候选先请主人指定，不静默选最新。
  - 已提交/正在发布的作品拒绝就地换稿；同一 artifact 版本已有排队/运行中的修订时拒绝第二次并发修改，避免两个 generation-2 版本互相覆盖。
  - `CreativeBrief` 记录父 run、base version 和 `feedback_message_id`；成功回执说明正在改哪篇、基于哪个版本、仍需重新批准。
- `bot/services/creator_agent/live.py`
  - `load_parent_artifact` 优先读取父 run 的 `creator_agent_materials` 和正文，保留 M ID；旧数据回退到 evidence 摘要。
  - 写稿证据从固定 900 字符摘要改为每篇最多 4000 字符（6 篇约 24K 字符，控制 prompt）；数字校验使用本地保存的正文全文，即使事实位于 4000 字符之后也能核实。claim 校验不再只拿摘要。
  - 修订使用 `purpose="revise"`，传入 `revision_contract`；标题/开头等局部修改由程序合并未授权字段；输出与父版本无可见差异时返回结构化 `no_change`，不谎称成功。
  - 校验异常转为 `DraftValidationError`，携带字段、类型、问题片段、动作和候选稿。
- `bot/services/creator_agent/registry.py` / `runner.py`
  - 工具异常保留 `issues`/`candidate`，runner 写入失败步骤观察；模型可在现有步数/模型预算内做定向修复，不新增循环。
- `bot/services/creator_agent/contracts.py`
  - `CreativeBrief.feedback_message_id`。

## 未完成 / 边界

- C 采用最小方案：仅识别标题局部、开头局部和“整体重写”，不是通用文档 diff/编辑语言。
- D 的结构化 issues/candidate 会进入失败步骤的 `recent_steps`，模型可在现有步数和模型预算内自行重试；没有新增自动修复循环，也没有放宽校验。
- 旧 `bot/services/drafts.py:revise_draft` 与旧 `revise` handler 仍在，供历史任务兼容；新“修改”命令不再路由到它。
- 未调用真实付费模型，未向飞书发送真实消息；真实链路需部署后由主人手机验收。
- 未迁移数据库、未重启服务、未提交或推送 Git。

## 测试

- `tests.test_creator_agent_live`：43 个用例，77.433s，`OK`（最终 4000 字符/全文校验版本）。
- 合并定向（creator live / creator v2 / approval publish / 飞书帮助 / publication agent / publication silence fix）：113 个用例，267.081s，`OK`。
- 全量：373 个用例，954.370s，`OK`；日志 `var/logs/full-tests-creation-revision.log`。
- 覆盖：带编号修改、V2 修改、无编号歧义、父 M 材料继承、900 字后数字、局部标题合并、保留图片 hash、无变化结构化 issue、registry 保留 issues/candidate、并发修改拒绝、在途发布拒绝、真实 inbound→worker→新审批→通知。

## 最小服务更新（尚未执行）

- 无需数据库迁移。
- 需重启 `gateway`（接收飞书“修改”命令）和 `worker`（Creator runner/审批生成）；console 不需要因本次改动重启。
- 更新前检查是否有在途 Creator/发布任务；有在途任务时不强停。
- 部署后手机验收步骤：
  1. 用一篇**未发布、待审批**的新稿（不要用已发布作品），发送：
     `修改 XH-XXXXXX 标题改短一点，正文扩写一点，保留第 1 张图`
  2. 预期先收到“已收到修改意见，正在修改 XH-…（基于版本 vN…）……仍需你批准，不会自动发布。”
  3. 等待新版本审批卡；正文/图片应为新版本，旧审批已变 `revision_pending`，不会自动发布。
  4. 若失败，应收到中文原因和下一步；不要重复发送“修复并重试”或直接重发旧稿。
- 不要用已发布的 XH-HMP6VB 做修改验收，也不要触发任何重发。

## 追加修复：修订输出上限与 JSON 有界重试（2026-11-19 23:52）

- 现场：`修改 XH-QVB4HM 如果能对比一下其他厂商模型就好了`（job 316）在 writer JSON 解析阶段失败，错误 `Expecting ',' delimiter: line 1 column 2310 (char 2309)`；审批仍 `revision_pending`，无发送记录。
- 原因：修订要追加厂商对比，响应在 2310 字符处结束；修订 writer 与初稿共用 1800 输出 token 上限。适配器只在 provider 返回 `finish_reason=length` 时提示截断，本次未返回该标记，所以落到 invalid JSON。
- 修改：
  - `bot/services/creator_agent/live.py`：修订 writer 单独使用 `budget.revision_max_output_tokens`，默认 2600；初稿仍用 `budget.max_output_tokens`。数字校验仍使用保存正文全文。
  - `bot/adapters/llm.py`：仅对 `writer returned invalid JSON` 做一次有界重试；重试仍走 `before_attempt` 和预算预留，并追加“只输出完整合法 JSON”的提示；截断、空响应和拒绝不做该重试。
  - `config/bot.live.toml` 与 `config/bot.example.toml`：新增 `revision_max_output_tokens = 2600`，原 `max_output_tokens` 不变。
- 测试：
  - 新增 JSON 重试成功、重试仅一次两个适配器测试；新增修订 writer 使用 2600 上限的测试。
  - `tests.test_config_budget tests.test_creator_agent_live tests.test_creator_agent_v2 tests.test_approval_publish`：100 个用例，183.953s，`OK`。
  - 全量：376 个用例，1048.396s，`OK`；日志 `var/logs/full-tests-json-retry.log`。
- 服务更新：2026-11-20 00:09 已只重启 worker（旧 31468 → 新 14136/29808），gateway 25340 与 console 31236 保持；无数据库迁移。重启前确认 live 无 pending/running job；`XH-QVB4HM` 仍 `revision_pending`，未重发、未发布。
