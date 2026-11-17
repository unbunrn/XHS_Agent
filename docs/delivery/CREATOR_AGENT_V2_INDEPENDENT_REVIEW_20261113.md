# Creator Agent V2 独立评估与后续优先级

日期：2026-11-13（Asia/Shanghai）。项目：本机 `D:\Dev\Projects\bot`。

## 结论

**模型驱动的创作 Agent 核心已形成，但产品闭环与可靠性仍不完整。** 可以作为课程原型展示动态工具选择；不能将一次成功生成/发布当作修改、审稿、恢复、定时运营都已完成的证据。建议停止增加角色/平台，先完成下述 A 组，再做 B 组。

本轮只读检查源码/交付文档、运行离线临时测试并编写本报告；未读取生产数据库、未调用真实模型、未发送飞书、未发布、未重启。交付报告称已部署并真实发布过一条 V2 笔记，本轮不独立确认该外部事实。进度文档仍写尚未批准，与统一交付报告相互矛盾，需统一事实来源。

## 独立验证范围

执行 `python -m unittest tests.test_creator_agent_v2 tests.test_creator_agent_live tests.test_migration_v2 -q -b`：**20 tests，32.371s，OK**。日志 `var/tests/parent-v2-review-20261113.log`。

额外反例：`var/tests/parent_v2_probe_20261113.py`，结果 `var/tests/parent-v2-probe-20261113.log`。使用 LiveRuntime + FakeLLM + 临时库，未连接真实平台。

交付方称隔离副本全量 340 项通过，主项目全量未完成。本轮没有重跑该全量，也不能把副本结果认定为主项目全量结果。现有测试对 FakeRuntime 的成功断言不能证明 LiveRuntime 等价。

## A：优先修复的闭环问题

### A1 / P1：V2 修改入口没有绑定旧作品

代码：`bot/services/approvals.py:485`、`creator_agent/entry.py::brief_from_text/accept_inbound`、`live.py:253`。

独立复现：发送 `V2 修改 少讲技术，保留第二张图` 后，job brief 为 origin=manual、topic=“修改 少讲技术，保留第二张图”、base_artifact_version=null、generation=1，生成新 run_key。已有 pending 审批未失效。

LiveRuntime.revise_artifact 也没有使用 revision_plan 的保留项，而是重新调用 _write；没有真实的指定图片保留逻辑。测试中 FakeRuntime 的图片哈希保持不能证明这个真实路径能保留。

修复：让修改命令明确定位 run/审批/作品版本；省略编号时只在唯一可判定的当前作品上应用，否则澄清。原子失效旧审批与创建新 generation，加载旧作品、材料、图片与原始 brief；保留资产 ID/哈希；后到的旧工具结果不得覆盖新 generation。保留普通 `修改 XH-...` 命令，但 V2 作品须路由到 V2 修订，不能又走另一套旧修稿流程。

验收：从真实入站处理函数到 LiveRuntime 和新审批完整验证，“保留第二张图”哈希不变，旧码不可发布，新反馈压过旧任务；已发布/publishing/unknown 的边界按既有规则处理，不假称撤销已发帖。

### A2 / P1：提交审批不幂等，副作用发生后才记 journal

代码：`live.py:310`、`runner.py:250`、`store.py::save_artifact`。

独立复现：同一 run/同一 artifact 连续调用 submit_for_approval，创建 draft_ids=[1,2]，pending_approvals=2。工具声明 idempotent=True 但实现没有唯一约束或查询已提交结果。artifact 内已保存 approval_code 也未阻止第二次创建。

runner 在 registry.execute 后才 append_step；幂等 key 没有作为执行凭据传给真实工具。崩溃发生在创建审批后、记录步骤前，重跑可能再次产生审批。此处证明重复审批风险，**不等同已发生重复真实发帖**，发帖仍需批准。

修复：副作用前持久化 action_id 与版本，提交桥以 run/generation/artifact_hash 唯一键占位/复用结果；草稿、关联、审批/outbox 之间采用事务或可恢复状态机。先确认已有批准结果，不因 artifact 表暂未更新就重发。不要将“防重复”只放在 FakeRuntime。

现有 save_artifact 使用 INSERT OR REPLACE，同版本渲染/提交会被覆盖；需区分不可变内容版本与渲染/提交状态，确保审批绑定的作品不是后来覆盖的对象。

验收：同版本重复提交返回同一个审批；在事务各边界注入崩溃，恢复后无重复审批/通知；过时 generation 无法提交。

### A3 / P1：审稿与事实验证弱于产品要求，提交可绕过检查

代码：`live.py:228/297/310`、`policy.py`。

独立复现：LiveRuntime.review_artifact 返回 pass，调用模型次数为 0；它只是 submission_issues 规则，不是交付目标中的独立语义审稿。

另一个反例：材料 quote 为“10 percent”，claim 为“999 percent”，claim_issues 返回 []。规则仅检查双方各自是否有数字，不比较值/单位，也没有确认 quote 真正存在于材料。_claims_from_content 只因存在 material ID 和正文就赋 supported；未证明确实支持主张。submit_for_approval 本身没有调用 submission_issues，也未要求该版本的有效 review；模型能直接选择 submit 跳过审稿。

修复：确定性检查放最终提交边界，无法靠跳过工具绕过；检查真实引用片段、数值/单位/版本、核心矛盾。独立模型审稿对 brief、反馈、正文、图文策划与证据进行语义检查。质量建议与关键事实问题分级，避免重新陷入“任何建议都卡稿”。review 必须绑定 artifact hash/generation，修改后旧 review 不可沿用。

验收：错误数字、伪造引用片段、核心矛盾阻止提交；合理媒体归因仍可交稿；用户修改意见由独立审稿验证落实；无 review 直接提交也不能越过关键规则。不要退回所有来源必须官方的老门槛。

### A4 / P1：人工来源与配额链路丢失

静态证据：LiveRuntime.submit_for_approval 调用 _insert_draft 时 topic_request_id=None；V2 不创建 legacy topic_requests 关联。现有 publish.py:338 依据同 event_id 的 topic_requests 判断 manual，选择人工或自动发布配额。

因此新的 V2 手动任务与既有人工/自动配额判别没有一致来源链路；不能用 brief.origin 仅写在 V2 表里就宣称旧发布路径已理解它。提交桥直接调用内部 _insert_draft 也需核对是否漏掉正常 create_draft 的生成配额/暂停前置检查。

修复：统一明确 task origin/lineage，将 V2 manual/scheduled/revision 对应到既有发布与生成政策，不依赖偶然的同主题 event。内部修订不能反复消耗首稿配额，手动稿也不能挤占自动名额。兼容历史稿件，不批量伪造 topic_requests。

验收：V2 人工/定时及多次修订在真实 quota gate 上的计数正确；并发提交不超额；pause_all 与账号禁用仍有效。此项本轮为静态链路发现，未触发真实配额/发布。

### A5 / P1：资源上限和恢复不完整

静态证据：

- runner 的 model_calls 只统计成功 agent_decide，不包含 LiveRuntime 写稿调用、失败调用及 adapter 重试。
- LiveRuntime 每次 search 创建新的 PhysicalFetchQuota，read_source 是另一条 HTTP 路径。tools.search/read_source 没有返回 physical_fetches，runner 的累计实际接近无效。
- _materials/_bodies 只保存在 LiveRuntime 内存；新 runner/context 启动时没有从持久化材料恢复。步骤记录的 history 不会自动重建写稿所需材料。
- deferred 被 runner 当作直接返回的终态，没有明确的续跑入口。checkpoint 与实际副作用不是原子边界。
- generation/base_version 可选检查不足：没有与数据库最新 generation 作提交前 CAS；声明有字段不等于能拦旧回调。

修复：每 run 共享、持久化的物理调用额度，所有模型/网络请求在 transport 入口记账；共享现有全局预算，不替换为假精确费用。恢复 material/body 引用及版本、动作状态；显式 resume 与预算恢复策略；提交和结果保存执行 generation CAS。失败与崩溃也计数。

验收：决策+写稿+审稿+重试合计受限；两个 search 的抓取额度不重置；抓完后重启能直接续写；预算耗尽后恢复不从零重搜；旧结果不能盖新版。

## B：达到用户目标仍缺的能力

这些不是新增花哨模块，而是既定需求没有接完：

1. **自主定时选题**：LiveRuntime.discover_topics 返回 []；scheduled_tick 有帮助函数但生产 worker 未见 V2 调度接线。继续走旧定时不等于 V2 会自主选题。复用现有 AIHot/来源发现，模型按账号定位、历史重复、可写性选一个，再进入同一 runner。
2. **澄清真正送达与接续**：LiveRuntime.ask_owner 直接 return None。当前 needs_owner 状态不能保证用户收到问题，也没有明确回答恢复机制。接既有 outbox，问题/答案绑定 run，不另起无关任务。
3. **图文改动与布局**：inspect_image 只检查文件和宽高，不能识别溢出、裁切或图文重复；render_images 的 layouts 参数未用于局部调整。submit 又重新渲染，可能使被审阅内部图片与审批图片不完全一致。增加实际布局测量、复用已审资产、保留指定图；无视觉模型继续诚实标注未做视觉审阅。
4. **偏好形成与纠正**：现有 preferences 查询/隔离测试只是存取基础设施，需接入用户反馈→局部/长期判断→可撤销记忆→下一任务应用，并保留适用范围。不要把一次修改永久化。
5. **时间元数据**：search/read_source 生成 Material 未填 published_at，_write 固定 date_unknown=False。应保留供应商返回的真实日期，未知日期显式传递；news/analysis 贯穿提交，不能靠丢掉日期避开时效。

## C：课程展示和体验改进的优先顺序

先完成 A，再补 B；暂不增加更多 agent、更多平台或微调以掩盖基本链路缺失。

- 做一份可回放的任务轨迹：用户要求、每步动作摘要、工具结果、错误恢复、作品差异、审批版本。不展示隐藏思维链或凭据。
- 用同一批约 24–30 个任务比较旧流程与 V2，包含“修改意见落实”和“非官方资料正常交稿”。记录事实支持、人工偏好、完成率、实际成本和时间；未运行真实模型评测前，不把 FakeRuntime 结果当效果提升。
- 面向用户减少 V2/旧链路双入口歧义：完成迁移后统一“选题/修改/状态”，版本号保留为调试信息。先修修改再合入口，避免把旧稳定功能同时破坏。
- 固定一次端到端离线验收矩阵，优先测 LiveRuntime 和生产路由，不靠 FakeRuntime 单独实现“理想行为”。全量挂起需要超时诊断/单测进程隔离，不能长期用另一副本通过替代主项目验证。

## 文档与清理

统一交付报告与 progress 的发布状态；只记录已授权且可验证的事实。交付报告包含带 xsec_token 查询参数的笔记链接，需改为不带该参数的公开笔记地址；本轮不复制该参数到新文档或提交仓库。README 同时称多个入口“当前任务”，应只保留一个当前入口，其余标历史。

不要现在大规模删旧链路：V2 的 scheduler/修订等还依赖旧能力。每完成一个替代切片再清理对应旧编排，发布/审批基础设施继续复用。清理要以真实生产调用图和回归测试为依据。

## 下一轮 DSH 建议

以 A1–A5 为一批可靠性与修订闭环任务，随后连续完成 B，不逐个小问题召回主控。先写失败测试再修，实现报告逐项关联证据；不只修文档或 FakeRuntime。禁止未经新授权读取秘密、收费调用、发送消息、重启、部署或发布。完成后一次性交付主项目离线测试、真实路由/LiveRuntime 测试、未完成项及迁移步骤。不要宣称系统“完全成熟”。
