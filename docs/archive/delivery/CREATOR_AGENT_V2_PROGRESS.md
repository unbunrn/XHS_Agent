# Creator Agent V2 — 进度

日期：2026-11-13  
范围：主项目已接入 V2；原隔离副本已清理。仓库现已初始化 Git，回滚以提交记录和本机备份为准。

## 已完成

1. 阅读并核对 `docs/DSH_CREATOR_AGENT_V2_TASK.md` 与现有实现。
2. 在隔离副本实现 V2 核心：`contracts.py`、`store.py`、`context.py`、`registry.py`、`runner.py`、`tools.py`、`policy.py`、`entry.py`。
3. 追加 schema migration 9（4 张 V2 表，不改历史迁移），并在 live DB 执行；`PRAGMA integrity_check=ok`。
4. 实现真实 `LiveRuntime`：搜索/正文抓取、写稿、Pillow 渲染、确定性布局检查、审稿、真实 pending approval 桥。
5. 主项目接入 `creator_agent_run` worker handler，以及带 `V2` 前缀的 owner 私聊入站路由。
6. 首次真实 V2 run `ca-ce0670cbe414c663b98b` 完成：8 步，1 次搜索，8 次模型调用，最终 `review_artifact=pass`、`submit_for_approval` 生成草稿 #17 / 审批 **`XH-2FMN8K`**。
7. owner 已批准 `XH-2FMN8K`，V2 发布成功；账号 feed 自动确认 note `6af69d16000000002903c3c1`，审批/草稿/尝试均为 `published`，`publish_unknown=0`。
8. 独立评估 A1–A5 主体修复：修订绑定/旧审批失效、父 artifact 与父 evidence 恢复、submit 幂等/不可变 artifact/draft 断点恢复、quote/数字校验、manual lineage、共享物理抓取/模型计数/generation CAS；A3 增加可选独立语义审稿开关（默认关闭，live 未重启启用）。未完成边界见下文。
9. T1–T4 已实现：观察面暴露父 artifact/材料/修订要求；最终图片 manifest 经 hash 校验写入 draft；generation CAS 前置、`_resume_existing_draft` 回填 lineage；migration 10 增加 `creator_agent_materials` 与 `drafts.submission_key/hash`，材料/正文跨重启恢复，`resume_deferred` 受配置控制，模型调用按 runtime 增量计数。
10. N1–N4 验收前收尾：`drafts.submission_key/hash` 唯一提交键与审批事务内取消/代际 guard；保留图片缺失/丢页/解码硬校验；重启后续接模型与物理抓取预算，预算类 deferred 仅显式提高上限或开启 `resume_budget_deferred` 才恢复。
11. 主项目 V2 targeted（R1/R2 返修 + 手机首轮修复后）：`tests.test_creator_agent_v2 + tests.test_creator_agent_live + test_migration_v2` → **48/48 OK**；加 `test_approval_publish` → **67/67 OK**（132.861s）。
12. 主项目全量（R1/R2 返修后）：**Ran 367 tests in 812.120s — OK**（`var/tests/full_suite_r1r2_final.log`）。这是手机首轮修复前记录，修复后尚未重跑全量。此前 N1–N4 基线为 359 项；副本全量 **340 tests OK**（642.312s）。手机首轮故障：小写 `v2` 前缀进入 topic、文本修订清空图片、失败 run 静默；已修并加回归。
13. R1/R2 复核返修：恢复提交前重算 immutable package hash，换图片/正文/顺序都拒绝；恢复只复用 key/hash 完全一致的 draft；最终审批事务内再次校验 run 状态/代际/账号、submission hash 与最终 images hash，并拒绝重复 pending approval。R2 在每次 provider/transport attempt 前检查并持久化单任务模型额度；decision/writer/语义审稿共用。
14. 新增真实入口穿透测试：`handle_inbound_message → creator_agent_run job → AgentRunner → revise_artifact → render_images → review → submit`；断言父 artifact/`P*` 材料进入观察、旧审批 `revision_pending`、新审批 `pending`、保留图 hash 不变。
15. migration 10 先在 live DB 临时副本演练（v9→v10、唯一索引、重复键拒绝、二次初始化幂等、integrity ok）；R1/R2 通过后已应用到 live，version=10，迁移后备份 `var/live/backups/post-r1r2-migration-20261114-0237.sqlite3`，worker/gateway 已重启。

## 尚未完成 / 边界

- A3 的独立语义审稿代码已存在，但默认关闭且 live 未重启用；当前 live 仍以确定性 claim/quote/number 校验为准。
- N1 已覆盖 draft 指针保存失败、审批已创建后崩溃、finalize 后崩溃，保持 1 draft/1 approval；N2 已在审批事务内拒绝取消后的提交；N3 已硬校验保留图片缺失、丢页和不可解码。R1/R2 已补恢复 package 变化拒绝、每次 transport attempt 预扣、重复审批/通知。剩余：语义审稿结果复用、checkpoint 与副作用完全原子化。
- T1 的真实 `handle_inbound_message → worker/AgentRunner` 父观察断言已补；不再只是 runner/live 级替身。
- `discover_topics` 仍返回空；热点发现继续走旧调度。
- 发布仍复用旧审批/发布闸门和 feed 回执确认；V2 没有另造发布器。
- 没有真实视觉模型；布局检查是确定性规则，`visual_review=not_available`。
- 旧模块保留为 live 回退路径；V2 没有删除历史迁移或生产数据。

## 下一步

- R1/R2 的主全量复跑结果回填交付报告；语义审稿结果复用、checkpoint 与副作用原子化边界仍保留为已知限制。
- migration 10 的部署/回滚演练已完成（临时副本）；R1/R2 通过后已应用到 live 并重启 worker/gateway，接下来做手机飞书验收。
- 补 B 组：scheduled discover、真实 ask_owner、layout/image preservation、preference memory、published_at 传播。
- 更新统一交付报告，明确“已发布的 V2 验证稿”和仍未完成的 A/B/T 项；migration 10 已按授权应用到 live，迁移后备份已保留。
