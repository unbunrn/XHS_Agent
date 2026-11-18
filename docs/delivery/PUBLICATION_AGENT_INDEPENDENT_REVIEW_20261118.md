# 发布恢复 Agent 独立审查与返修要求

> 2026-11-18 返修状态：R1–R6 已在 `docs/delivery/PUBLICATION_AGENT_DELIVERY.md` 中逐项修复并补充反例/穿透测试；本文件保留原始审查发现作为历史记录。功能仍关闭，未部署。补充 handoff 反例后最新定向 **71/71**、主全量 **333/333** 通过；详见返修报告和 `docs/delivery/PUBLICATION_AGENT_HANDOFF_DELIVERY.md`。

更新时间：2026-11-18T00:23:00+08:00。
项目：D:\Dev\Projects\bot。基线需求：docs/DSH_PUBLICATION_RECOVERY_AGENT_TASK.md。
结论：框架已实现，核心恢复/取消语义未闭环；不通过部署验收。仅离线检查，不操作生产。

## 验证范围

阅读 publication_agent 全包、publish.py、publishers.py、worker、research 来源同步及交付记录。
交付方全量日志 full_suite_publication_matrix_final9.log 记录 302 tests / 465.024s / OK，仍有 asyncio ResourceWarning。这不是本轮独立全量结果。
独立故障探针：var/tests/parent_publication_probe_20261118.py，结果 var/tests/parent-publication-probe-20261118.log。临时数据库 + FakePublisher，无真实 POST。
独立相关回归日志：var/tests/parent-publication-review-20261118.log；最终结果见文末。

已实现的有效部分：publication_tasks 与步骤表、approval 入队分流、PublicationRunner 的模型动作选择、工具注册、HTTP 发布闸门复用、规范飞书命令、基础租约、同任务多 attempt 标识，以及一部分穿透测试。这些可以保留，无需推翻重写。

## R1（P0）：旧错误分类叠加新自动重试，可能重复发帖

证据：publishers.py:655 起仍把 5xx 且短时间 feed 未查到匹配笔记归为 PublishFailed(retryable=False)。publication_agent/live.py publish 对所有 state=failed 均 bump_retry 并 schedule，既未区分 dispatch_state，也未遵守 retryable=False。policy.py 的分类与计划函数虽存在，实际自动重试分支没有使用它们。

因此旧系统原本“不自动重试”的失败，在新 Agent 下会自动重试；平台已接收但 feed 延迟的情况可能重复发布。此项由代码路径确认，本轮未向真实平台复现。

必须修：5xx/发送后读超时/无回执默认 unknown；仅明确未发送或有明确拒绝证据且允许恢复的结果能进入发送重试。发布适配器、PublishOutcome、publish_attempts 和 runtime 贯通结构化分类，不用错误字符串猜测。登录/资产/拒绝也不能统一当连接失败。补 adapter → gate → agent 的 500+延迟出现笔记测试，断言 POST=1。

## R2（P0）：暂停与取消不是最终执行约束

探针实际结果：发送“暂停发布 XH-...”后执行已排队 runner，state=published、POSTs=1。
原因：runner._run_locked 只排除 published/cancelled，随即无条件将状态改为 diagnosing；paused/waiting_* /未来 retry_wait 都可能被旧 job 激活。

同时 publish.py 最后事务未核验 publication task 的取消、暂停、generation、lease/fence 和冻结包关联；工具层只检查旧 context.task.cancel_requested，不能保护“工具检查后、claim 前取消”的竞态。租约只获取/释放，没有续租与最后提交 fencing。

必须修：执行资格由事务内状态机判断；等待仅能被匹配的有效事件唤醒，未来任务不得提前执行。claim 内复检 task/approval/account/artifact 对应关系、包 hash、generation、取消/暂停和 lease token。取消/暂停需使旧执行者失效；lease 过期不是旧执行者继续发送的许可。检查 schedule_resume 不能覆盖取消/暂停。

验收：暂停后旧 job POST=0；取消发生在工具预检后仍 POST=0；两执行者、租约过期与旧执行者恢复不会双发。

## R3（P1）：未知结果没有平台对账，取消会过早终结

探针实际结果：第一次发布得到 unknown，随后“取消发布”，task_state=cancelled，但 attempt_state=unknown。
原因：_finish 清 active_attempt_id 后，entry.handle_command 只凭 active_attempt_id/当前 submitting/verifying 判断，未查询 unresolved attempts。

live.check_result 仅 SELECT 本地 publish_attempts，不调用 bridge/feed/reconcile_readonly；所谓“查询发布结果”没有获取新证据。owner “确认未发布并重试”直接改 unknown→failed，没有执行需求要求的晚到成功复查。

必须修：check_result 接入只读 EvidenceProvider/现有 reconcile_readonly，持久保存证据；unknown 取消仅 cancel_requested，继续只读对账。成功晚到则 published 并说明取消未能撤回；不得自动删帖。人工确认未发布需绑定当前 attempt/generation，执行新发送前再次尽可能核对，出现成功证据就阻止重发。查询无结果不等于失败。

验收：远端已成功、本地 unknown 后用查询工具补记成功，POST 不增加；unknown 取消不终结；晚到成功优先于人工旧回复。

## R4（P1）：持续工作与预算恢复不成立

探针实际结果：max_model_calls=1，已有一步后发送“继续发布”，仍 waiting_owner/model budget exhausted，POST=0。

runner 用历史 steps 数充当模型调用总数，既不是真实 transport attempts，也没有恢复批次/每日额度；complete_json 没传已有 before_attempt 钩子。非法输出、网络重试与崩溃前调用无法准确持久计数。超限/非法决策直接等待，未可靠排队一条待协助通知。

此外 wake_task 的 dedupe key 对 login/resume/retry 使用固定 generation，而这些命令不递增 generation。同一任务第二次“已登录”或“继续”可能命中已经 done 的旧 job，无法真正唤醒。

必须修：区分每轮/恢复批次/日累计预算，实际网络尝试前持久预留；owner 明确继续创建受限新批次，保留总消耗。唤醒以有效 inbound message/恢复事件 ID 唯一去重，同一消息不重复、新消息能恢复。waiting_owner/登录/超预算有一次确定性 outbox 通知；服务异常不静默吞进 worker 返回值后把 job 标完成。

验收：多轮登录/继续、同消息重放、超预算通知、重启后调用预算、transport 重试；耗尽后用户新恢复授权能运行，但不绕过全局费用限额。

## R5（P1）：资产恢复没有可用的受限备份闭环

restore_approved_assets 要模型自行提供 backups 路径；缺少按批准包建立备份清单及可用备份观察。diagnostics.restore_same_bytes 只约束目标 roots，不约束源备份 roots；src.resolve 后才 is_symlink 也无法正确识别原始符号链接。写入使用直接 write_bytes，非原子恢复；工具无论有无恢复都 ok=true。

必须修：批准资产的不可变备份清单由程序维护，模型只传资产 ID，程序选择同 hash 备份；主机/容器映射分开诊断。源目标均限制项目根和解析后的边界、拒绝不可信链接，临时写入核验后原子替换。恢复后 verify 必须通过才成功；无备份保持等待，禁止重画替代。增加真实缺文件→恢复相同字节→发布的穿透测试，不能只测路径越界。

## R6（P1）：需求明确要求的基础修复仍遗漏

- research.sync_sources 仍按旧 topic_requests 名称保留 commissioned 来源，V2 临时来源误禁用问题未解决。需归属映射和专门回归，不得全面 enabled=1。
- 发布配额仍 COUNT publish_attempts；同一篇安全重试继续消耗作品名额。按任务/包统计作品占位，另设请求尝试上限，保留自动/手动区别及 unknown 占位。
- publication_tasks 仍按 approval_id+包（包又包含 approval_id）去重，不能阻止同一作品换审批编号后创建并行任务。增加作品语义身份与跨审批单活/成功检查。
- 包核验需覆盖最终 title/tags/visibility/有序图片，不能把“保存了 JSON”当作“claim 时已验证该包”。
- simulated 被映射为 task.published；模拟状态必须明确分开，不能让界面声称真实发布。

## 其余必须收尾的需求缺口

1. “检查原因”当前只是返回本地状态，不唤醒诊断 Agent；应允许只读诊断，并确保它不意外变成发布入口。
2. notify_owner 可让模型发送任意字符串，没有证据约束/统一脱敏；通知应由结构化状态生成或校验，不能编造成功。inspect_failure/异常/账号响应进入模型前脱敏。
3. 桥接阶段日志、截图、toast 和 2 分钟 deadline 调查仍未交付。否则 Agent 看不到失败根因，只能反复读同一条错误。代码路径和能力边界如实记录。
4. owner_requests 未完整关闭/过期处理，自然语言和无编号唯一目标解析未实现。后者可独立小阶段，但报告不能写已完成全部 S0–S5。
5. 部署方案仅停 worker 不足以保证无 DB 写入（gateway/console 也可能写库）；备份/回滚必须有维护窗口，包含代码与配置兼容。不得上线后直接恢复旧 DB 丢掉上线后审批/发布事实。

## DSH 下一步与验收顺序

保持 publication_agent.enabled=false；不部署 migration 11，不重放历史失败稿。

第一批修 R1–R3（最高优先）：真实 adapter 结果分类、暂停/取消最终闸门、真正平台对账。加入本轮三个反例并扩展时间交错测试。
第二批修 R4–R6：可恢复预算/消息、资产恢复、来源/配额/跨审批身份。不再只让 FakePublisher 直接返回 failed 验证所有重试。
第三批补通知/诊断/owner 接续与文档，运行任务书完整矩阵及主项目全量，临时库迁移回滚演练。

新的交付报告按“已实现、独立测试范围、未实现、未部署”写，明确每项证据；不把 302 项通过当作需求全部完成，也不要只新增更宽松测试。

## 本轮独立回归最终结果

执行 tests.test_publication_agent、tests.test_publication_recovery_matrix、tests.test_publication_schema10_compat、tests.test_migration_v2、tests.test_approval_publish：46 项通过，215.400 秒，退出码 0。日志 var/tests/parent-publication-review-20261118.log。

这说明现有测试通过，不代表上面额外复现的问题已修复。三个独立探针输出分别是 paused→published/POSTs=1；budget_resume→waiting_owner/POSTs=0；unknown_cancel→task cancelled/attempt unknown。检查范围内不通过部署验收。
