# Creator Agent V2 修复复查与下一步

更新时间：2026-11-13T18:50:11+08:00。环境：当前电脑 D:\Dev\Projects\bot。

## 结论

本轮修复有进展，但尚未完成 V2 验收。不要把交付文档中的“A1 已修 / A2 核心已修”理解为完整的修改与恢复链路已通过。下一步先修下面两个可复现问题，再完成持久恢复和受控手机验收；暂不扩展多智能体或重做发布器。

本轮只检查源码、交付记录和离线测试；未部署、重启生产、调用付费模型、发送飞书消息或发布帖子。交付文档中的历史 live 状态不属于本轮独立验证结果。

## 本轮证据

- 独立运行 tests.test_creator_agent_v2、tests.test_creator_agent_live、tests.test_migration_v2、tests.test_approval_publish：43 项通过，80.848 秒。日志 var/tests/parent-v2-rereview-20261113.log。这不是主项目全量回归。
- 附加故障探针：var/tests/parent_v2_rereview_probe.py，输出 var/tests/parent-v2-rereview-probe.log。使用 FakeLLM、临时测试库，不触碰生产。
- 同一作品正常完成提交后重复提交：draft_ids=[1,1]，确认此场景已修复。
- 修改入口现在标记 origin=revision、base_artifact_version=1，并使旧审批进入 revision_pending；但 worker 上下文没有原作品和材料，见问题 1。
- 中断后重试探针以 sqlite3.IntegrityError 结束，属于确认的失败证据，不能算测试通过，见问题 2。
- 确定性数字/引文校验及提交检查已有修正；独立模型语义审稿仍为可选，默认关闭。当前配置生效和真实审稿质量未验证。

## 必修 1：修改任务没有真正取得原作品（P1）

位置：bot/services/creator_agent/entry.py:134、context.py:29、tools.py 的 revise_artifact 路径、worker 中 creator_agent_run 上下文构造。

复现：构建已提交作品，经真实 handle_inbound_message 输入“V2 修改 少讲技术，保留第二张图”，再按 worker 方式从队列构造 brief/context。

结果：origin=revision，base_version=1，但 topic 变成原 run_key（ca-live-test）；新 run_key 下 latest_artifact_available=false，material_count=0。原 run_key 只在 accept_revision 的返回值中，没有以可恢复父作品引用进入任务。tools 修订调用使用 context.latest_artifact()，因而无法取得应修改的原稿。

影响：入口看似接收了修改，但后续不能可靠对照原稿、原材料和原图片执行。旧审批已冻结，用户可能只能等到失败或得到重新写作结果。

实现要求：

1. 明确持久化 parent_run_key、parent_artifact_version（或不可变作品 ID），保留原始 topic；新 run 与新作品版本的关系必须明确。
2. worker 从持久存储加载父作品、证据材料和图片引用，作为可读基线；不得直接覆盖父版本。
3. 按账号选择目标；支持明确审批编号，省略编号时只在当前账号内无歧义地选择。
4. “保留第二张图”要落实到指定资产及文件 hash。当前 revise_artifact 复制全部 image 记录仍不等于最终图保留；检查后续 render_images 和提交时重渲染，确保不会覆盖被保留的图。
5. 将用户意见交给模型形成修改计划和修改前后差异，不依赖两个固定短语完成需求理解。

验收：经过 inbound → job → worker/runner → 修订 → 再审批完整离线路径；确认模型取得旧正文/材料，未要求修改的第二张图 hash 不变，改动部分确实变化，原版本不变，旧批准不可发布。另测跨账号和歧义目标。

## 必修 2：提交中途失败后不能恢复（P1）

位置：bot/services/creator_agent/live.py:370、421–499。

复现：在 _insert_draft 与保存 artifact.draft_id 之后、_render_and_store 处注入异常；从 store 重新读取 artifact 并重试 submit_for_approval。

结果：_existing_submission 只认已经有 approval 的 draft；没有 approval 就返回 None，随后重新 _insert_draft，触发 UNIQUE constraint failed: drafts.topic_request_id。

影响：正常完成后的重复提交已幂等，但“草稿已建、审批未建”的中间态仍卡住。此证据是恢复失败，不是已经造成重复真实发帖。

实现要求：

1. 以 run/作品版本建立持久唯一提交标识，查到既有 draft 后继续渲染/审批阶段，不能再次插入。
2. 明确 prepared/rendered/approval_created 等可恢复阶段；数据库事务用于本地原子状态，文件与外部通知用幂等步骤/outbox，不要持有长事务执行外部请求。
3. 只有最终审批已建立后才标记 submitted；并发重试也只能建立一套 draft/approval。
4. 同时解决材料跨进程持久化：当前探针使用同一 runtime 已失败，真正重启后还必须重建材料上下文。

验收：分别在 draft 创建、渲染结束、审批创建、返回结果前注入中断；使用新 runtime/worker 和同一测试库恢复，最终只有一个 draft、一个有效 approval、一次完整通知任务，既有发布状态不被重置。

## 已声明但尚未完成的范围

交付文档本身承认：材料/正文持久化、deferred 恢复、checkpoint 与副作用恢复尚未完成。代码中 discover_topics 返回空，ask_owner 返回 None。因此“模型自己发现选题”和“向主人提问再继续”的 V2 路径还没有闭环；旧调度仍运行不等于 V2 自动选题已接通。

还应完成：所有实际模型调用与重试计费/计数的一致性、真正原子化的 generation 检查、布局检查与最终发送资产一致、材料发布时间传递。偏好长期学习可后置，先把现有修订做实。

## 下一步安排（供 DSH 执行）

### 第一批：交付可验收的手动创作闭环

修复上述两项，补材料/父作品持久化、断点恢复。启用并验证受预算约束的独立语义审稿，明确最大迭代数与失败回退；不要重新引入“必须官方域名才允许生成”的全局硬门槛。让每条关键事实得到证据、归因或不确定性处理；资料不足时收窄论断、补一次研究或询问用户，不能无限搜索。

完成后运行相关测试和主项目全量回归，记录实际命令、通过/失败/跳过数及日志。不要用历史测试数代替当前结果。增加上述穿透入口和故障注入的测试，而不是只断言 brief 字段。

### 第二批：受控手机验收

第一批通过后再部署。备份生产库、记录代码版本与回滚方式，维持现有发布审批边界。依次测试：给链接生成一篇 → 对它提出带保留条件的修改 → 生成过程中重启后继续。验收正文变化、图片保留、重新审批和不重复通知；这些通过后才选择一篇最终稿批准真实发布。不要为验证自动发布未经用户批准的内容。

### 第三批：补自主入口与课程评估

接通定时发现/去重/自主选题和 ask_owner 的通知、回答关联、超时与恢复。之后做小规模固定选题评估：旧工作流与新 Agent 在相同题目上的内容质量、修改遵从、证据充分度、模型调用数/费用和完成率。保存脱敏的工具轨迹与版本差异，形成课程展示材料；不要先堆多智能体数量。

## 文档维护

README 仍混有历史“174 项通过”和“尚未真实验证”等快照；它们不代表当前状态。DSH 完成后应更新一份有日期和证据链接的当前状态入口，历史报告保留并注明时间，避免使用者误判。当前项目没有可用 Git 仓库元数据，本轮不能报告提交编号。
