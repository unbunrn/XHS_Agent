# V2 第五轮独立复核（2026-11-14）

更新时间：2026-11-14T01:03:00+08:00。
环境：当前电脑 D:\Dev\Projects\bot。范围：N1–N4 本地实现与离线验证，不是生产验收。

## 结论

上一轮问题已有实质修复，但暂不建议部署。现在聚焦两个具体问题，不追加架构重构或功能扩展。

已核对：提交唯一键找回 draft；取消/代际在最终审批事务内检查；保留图片缺失和丢页报错；持久计数恢复。独立运行 V2、live、migration 三个测试模块：37 项通过，64.087 秒，日志 var/tests/parent-v2-round5.log。DSH 的 full_suite_n1_n4_final.log 记录 359 项通过（819.019 秒），仍有 unclosed event loop 警告；这是交付方全量结果，本轮未独立重跑全量。

交付方报告 migration 10 尚未在 live 执行。本轮没有生产迁移、重启、飞书发送、真实发布或付费模型调用。

## 必修 R1：恢复分支的提交包校验比较错了对象（P1）

文件：bot/services/creator_agent/live.py 的 _resume_existing_draft、_finalize_guard、_existing_submission。

独立探针：var/tests/parent_v2_round5_probe.py，日志 parent-v2-round5-probe.log。

复现：创建 draft 后，在 _persist_artifact_images 前注入异常；重新加载 artifact，将图片清单换成另一张合法 PNG 并填写它的正确 hash，再重试。输出 package_changed=true、ok=true、approvals=1。

原因：恢复路径取 draft 已保存的 submission_hash 作为 expected_hash，guard 再将 draft.submission_hash 与该值比较，实际上比较的是同一个旧值。恢复时传入的当前 artifact/图片包没有与原提交包比较；新图片已写入 draft_images，但仍通过旧 hash 的 guard。这不是已经发现真实误发，而是独立复现的检查缺口。

修复：

1. 文件复制和 draft_images 写入前，计算当前不可变提交包 hash，与原提交记录比较。恢复只能继续同一包；变化必须创建新作品版本并重新检查、审批。
2. 最终事务同时验证原提交记录、当前待提交包、最终 manifest 的一致性；不要用原值自比较代替内容校验。
3. _existing_submission 的快速复用路径同样核对版本和包身份，不能为变化后的内容返回旧审批。
4. 并发 finalize 同一提交时，在事务内复检已有审批，保证只有一套有效审批和通知任务。

验收：本探针改为拒绝且 approvals=0，原包仍能恢复；正文/图片顺序/hash 改变均不能复用旧审批；同包重复调用保持一个 draft/approval。加入两个独立执行者交错进入 finalize 的测试，以及通知事务失败回滚测试。不要删除成功审批来替代这些场景。

## 必修 R2：run 调用上限仍不是实际调用前的硬限制（P2）

文件：live.py _reserve_model_call；runner.py 的模型调用和工具执行；adapters/llm.py 的 transport 重试。

源码证据：_reserve_model_call 只有 self.model_calls += 1，不检查剩余上限，也不持久预留。runner 仅在循环开头判断总数；若剩一次额度，agent_decide 消耗后仍可选择 writer/critic，产生额外请求。transport 在一次 complete_json 内重试多次，当前 run 计数没有逐请求接入。交付报告“包含 transport 重试”不被当前代码支持。

注意：原有 BudgetGuard 在每次网络尝试前进行日预算预留，这条货币预算保护仍存在。本问题是 Creator Agent 单 run 的 max_model_calls 和崩溃恢复计数，不能误写为全项目没有预算。

修复：

1. 为单 run 建立共享的持久调用预留机制，决策/writer/critic 和 transport 重试都在真正请求前检查并占用额度。
2. 决策用掉最后一次额度后，不能再进入付费工具请求；直接保存可恢复状态。
3. 请求前预留的记录跨崩溃保留，未知结果按已消耗处理；不要仅在工具结束后 checkpoint 才持久化。
4. 明确配置的含义：如要另设“逻辑模型步骤上限”，与“实际 HTTP 尝试上限”分别命名和计数，报告不得混用。

验收：剩余一次时强制决策选择 writer，验证 transport 总尝试数仍不超限；注入一次重试；预留后进程中断再恢复。测试应观测请求次数，而非仅调用 _reserve_model_call 并断言数字加一。

## 一次性交付范围与验收门槛

- 完成 R1/R2 和已承诺的真实 inbound → worker → revision → approval 测试，验证父稿/材料实际进入决策请求、最终保留图片 hash 正确。
- 完成通知唯一性测试。语义审稿结果缓存可后置为成本优化，不必因此无限延迟手机验收；但没有缓存时也必须遵守真实调用上限。
- 跑相关回归与当前主项目全量，记录实际结果，修复测试事件循环未关闭警告。
- 准备 migration 10 在临时旧库上的升级与回滚演练记录，保持 live 不变。部署时使用本次部署前新备份，不能依赖历史旧备份替代。
- 更新交付报告，不把“字段和方法已存在”表述为“边界已通过”；旧 T/N 章节中过时的剩余项同步修正。

满足上述门槛后，建议下一步进入受控部署与手机验收，不再追加大规模改造。手机验证：生成一篇 → 指定修改并保留第二张图 → 重启恢复 → 旧审批失效 → 用户批准最终稿。定时自主选题、ask_owner 和长期偏好学习留下一功能批。
