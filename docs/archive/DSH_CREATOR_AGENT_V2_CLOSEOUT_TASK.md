# DSH 下一批任务：V2 手动创作闭环收尾

更新时间：2026-11-13T19:46:00+08:00。
项目：当前 Windows 电脑 D:\Dev\Projects\bot。
依据：19:38 更新的 CREATOR_AGENT_V2_DELIVERY.md / PROGRESS.md，以及本轮源码审查和离线探针。此文接续 CREATOR_AGENT_V2_REREVIEW_20261113.md，不表示旧验收要求已全部完成。

## 审查结论

本次修复有效，但第一批任务仍未完成，尚不能进入完整生产验收。不要继续追加多智能体、长期偏好学习或新平台；先完成本文件范围。不要把“方法中有逻辑”当作“入口到最终审批已通过”。

已确认的进展：

- 修改任务持久化 parent_run_key，保留原选题；worker 加载父 artifact 和父 event evidence。按 worker 构造的独立探针得到 has_parent=true、materials=1。
- 修改目标查询增加账号过滤及审批编号过滤（源码核对）。
- 模拟渲染前中断，重新创建 LiveRuntime 后重试，成功复用同一 draft：drafts=1、approvals=1，上一轮 UNIQUE 错误在这个场景不再复现。

独立证据：var/tests/parent_v2_round3_probe.py、var/tests/parent-v2-round3-probe.log。全部使用 FakeLLM 和临时数据库，没有调用真实服务。正常与过期版本恢复的差别见下文。

DSH 报告的 44 项通过是交付方结果；本轮独立回归结果另见文末。副本旧 340 项结果不能替代当前主项目全量结果。

## T1：把父稿与材料真正交给决策模型（P1）

文件：bot/services/creator_agent/runner.py 的 _observation，context.py，worker.py。

现状：worker 已加载父稿和 P<id> 材料，但 _observation 仍只查 store.latest_artifact(brief.run_key)。新修订 run 尚无作品，故模型看到 latest_artifact=null；返回的 observation 也没有 context.materials 的清单。工具能从 context 取父稿，不等于做工具选择的模型看得到父稿及可用材料 ID。当前修改测试手工传入材料 ID，掩盖了这个缺口。

任务：

- observation 区分 parent_artifact 和当前产物，提供受长度约束的原正文、图片编号/用途/资产 ID、材料 ID/URL/必要证据摘要、原始修改意见及保留约束。
- 新版本产生后显式更新 current_artifact；不能误把父稿直接作为新版本提交，不能覆盖父版本。
- 父证据重建时保留原材料 ID 映射或提供明确映射，避免父 claims 引用 M1、恢复材料变 P1 后被错误判无证据。
- 同一条用户修改请求不得依赖模型猜测隐藏的材料 ID。

验收：用真实 inbound → job → worker/runner 路径和录制式 FakeLLM，检查第一次决策 payload 可见父正文、修改意见及可用材料 ID；让模型据此选择 revise_artifact，最终产生新的可审批版本。禁止绕过 worker 手工调用 revise_artifact 代替此测试。

## T2：最终图片必须兑现保留要求（P1）

文件：live.py 的 revise_artifact、render_images、submit_for_approval、_resume_existing_draft；drafts.py 的 _render_and_store。

现状：revise_artifact 遇到 preserve 就复制所有图片记录；render_images 又会覆盖全部图片，submit 和恢复路径调用 _render_and_store，后者从正文重新 render_draft，完全不使用 artifact.images 中保留的文件。现有测试只检查 revise_artifact 返回时的 hash，未检查最终 draft_images / 通知资产。

任务：

- 用明确的资产 ID/编号表达保留、替换、新增；保留第二张不应等于冻结全部图片。
- 把最终图片清单作为不可变提交包。渲染只处理变更图片；提交注册已完成的资产，不再无条件重画。
- 审稿、布局检查、飞书审批通知和最终发布引用同一份文件及 hash；资产变化使既有检查/批准失效。
- 必要时复制保留资产到新版本目录，保留内容 hash，不依赖会被旧版本清理的临时文件。

验收：至少两张内容不同的图片。用户要求“改第一张，第二张原样保留”，经过 revise → render → review → submit，断言最终 draft_images 第二张 hash 等于父版本，第一张符合修改要求；审批通知引用最终资产。另测重启恢复。不要只比较中间 artifact 的字典。

## T3：恢复路径也必须检查版本有效性并补全关联（P1）

文件：live.py:459 起 _resume_existing_draft，以及 submit_for_approval 的检查顺序。

独立复现：在 draft 已建、渲染前抛异常；将 creator_agent_runs.generation 从 1 改为 2；用新的 runtime 对旧 generation=1 重试。结果仍 ok=true、approvals=1。原因：_resume_existing_draft 位于 generation 检查之前，直接渲染并建立审批。

另一个确定问题：正常恢复后 topic_requests.approval_id 仍为 null，恢复分支没有正常提交分支的 lineage 回填。

任务：

- 所有会新建或改变审批的恢复分支，都先核验账号、作品版本、generation、取消/被替代状态和有效检查凭据；不要靠普通读后比较冒充 CAS。
- 生成旧版本的任务不得在新修改已接受后恢复出可批准旧稿。事务内复检，并提供并发版本切换测试。
- 完成同一提交包的 draft / approval / topic_request 关联、result_code 和通知任务写入，做到重复执行幂等。
- 崩溃发生在 draft 插入、artifact 指针写入、渲染完成、approval 建立、通知入队等不同窗口时均可恢复；不能仅删除一个成功审批模拟全部崩溃。
- 对缺失/过期检查凭据的恢复执行必要复检；已有且与相同内容/资产 hash 绑定的有效检查应复用，避免无谓重复模型调用。

验收：当前探针中 stale_generation=true 必须被拒绝且不得新建 approval；正常恢复保持唯一 draft/approval，topic_requests 的 draft_id/approval_id/result_code 完整；旧批准及 publish_unknown 不得重置。

## T4：完成已承诺的持久运行与有界语义审稿

这一项是上轮第一批未完成范围，不是新增需求。

- 持久化搜索材料、必要正文/摘要、来源时间/角色、材料 ID 和 claim 关联。新 runtime 在搜索后或写作后恢复，不需要重新抓取全部来源。父 event 摘录恢复不能代替尚未提交 run 的材料持久化。
- deferred 要有明确可恢复条件、恢复入口和预算重置规则；取消/过时代不能复活。
- 决策、writer、critic、重试均通过统一模型调用计数与预算接口。当前 runner 只额外读取 writer_model_calls，语义审稿直接 complete_json，不能证明它纳入 max_model_calls。
- 开启语义审稿前先完成离线覆盖；生产配置变更与部署作为单独交付步骤记录。相同内容/证据/资产 hash 的审稿可复用，避免 review 工具和 submit 重复审相同稿件。
- 事实不足时，模型可缩小论断、归因、标注不确定性或询问用户；保持有限补搜/修改次数，不引入全局官方域名硬门槛，不允许无限兜圈。

## 测试与交付要求

1. 把上述穿透入口、最终图片 hash、过期版本恢复和新 runtime 材料恢复用例加入正式 tests。
2. 运行主项目相关回归与全量回归。若全量挂起，启用逐项输出/超时堆栈定位具体测试，修复根因或给出明确可复现环境阻塞；不能继续仅引用副本结果。
3. 更新统一报告，按“已实现 / 离线验证 / live 验证 / 未完成”分开写。PROGRESS 中“A1–A5 已修”和后文“A5 未完成”相矛盾，必须消除。全量段落中的旧“20 项”也需校正。
4. 提供实际改动文件、测试命令与日志、未完成项、部署和回滚步骤。日志脱敏，不附生产密钥/带令牌链接。
5. 不删除旧发布闸门、历史迁移、生产数据库或备份。本任务不授权额外真实发帖、消息发送或自动批准，也不要求现在重启生产。

## 此批通过后再做什么

先让 Codex 独立复查，再部署并安排手机三步验收：给选题生成 → 修改且保留指定图片 → 中途重启后继续。通过后由用户批准一篇最终稿验证发布。

然后才进入下一批：定时自主选题与去重、ask_owner 问答恢复、课程展示用对照评估。暂不堆多智能体或做微调；可用稳定工具调用轨迹、修改前后对比和质量/调用成本评估说明本项目的 Agent 能力。

## 本轮独立回归最终结果

命令：`.venv/Scripts/python.exe -m unittest tests.test_creator_agent_v2 tests.test_creator_agent_live tests.test_migration_v2 tests.test_approval_publish -q -b`。

结果：44 项通过，115.530 秒，退出码 0。日志：`var/tests/parent-v2-review-round3.log`。这是四个相关模块的回归，不是全量通过；额外故障探针暴露的过期版本恢复问题不在现有测试覆盖内，因此 44 项通过不改变上述收尾结论。
