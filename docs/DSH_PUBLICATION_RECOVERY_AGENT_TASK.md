# DSH 开发任务：小红书发布与故障恢复 Agent

版本：需求基线 1.0
编写时间：2026-11-17T21:02:00+08:00
项目位置：当前 Windows 电脑 D:\Dev\Projects\bot
开发执行器：D:\DeepSeek_Harness；开发模型由用户在 DSH 选择，不把其 UI 型号名称硬编码为运行时 API 模型 ID。

## 0. 先读：本次要交付的用户体验

用户希望同一篇已批准作品由 Agent 负责到结果确认：Agent 发帖、查结果、飞书通知；出问题后理解用户的“检查原因”“修复后重试”“已登录”“停止发布”，继续原任务。只有确认发布成功或用户明确取消，任务才正常终结。

这不是增加几条重试规则，也不是把已有 publish(job) 改个名字。必须有模型根据真实观察选择诊断/恢复工具，工具结果进入下一轮，任务跨人工等待和进程重启保持身份与上下文。

同时，“持续负责”不等于一直运行模型或盲目重复 POST。等待登录、额度、外部服务、人工确认时，持久保存任务并释放 worker；到期唤醒或收到回复后继续。不能保证外部平台一定接受发布，无法完成时明确说明阻塞原因并保留任务。

本次开发以本文为主，历史 TASK 文档只作背景。允许重构、删除证实无用的旧代码，但不能破坏创作、审批、账号身份校验、对账及现有生产数据。不要继续重做内容创作系统，也不要引入点赞、评论或新平台。

## 1. 当前项目事实与集成位置

以下依据本轮源码检查，不代表修改后状态：

- Creator Agent：bot/services/creator_agent/{runner,tools,entry,live,store,contracts}.py。主要创作工具在 submit_for_approval 后结束。
- 飞书入口：bot/services/approvals.py handle_inbound_message；网关 bot/lark_gateway.py。批准/修改/拒绝与入站去重已存在。
- worker：bot/worker.py；持久队列 bot/jobs.py。已包含 creator_agent_run、publish 等 handler，并有启动恢复。
- 唯一真实发布边界：bot/services/publish.py，核验批准、版本、hash、账号、来源、配额，先落发布尝试再调用适配器。
- HTTP 桥接适配器：bot/adapters/publishers.py XiaohongshuPublisher，调用本机登录、user/me、publish 接口。当前不需要换 MCP。
- 发布结果：bot/services/publication_results.py；只读对账：bot/services/reconcile_readonly.py；通知：bot/services/notifications.py。
- 数据库已有 approvals、publish_attempts、publish_results、publish_reconcile_checks、jobs、notifications、Creator Agent 表。源码当前最高迁移为 10；开发开始时重新确认最新编号，只追加下一迁移，不修改已应用的历史迁移。
- 不假定“目前所有版本都可回滚到旧备份”。部署前需针对实际运行代码、数据库和桥接镜像重新做备份。

### 最近失败必须一起解决

1. 本轮此前只读排查发现：草稿 #28 的 event 25 关联来源被置为 enabled=0，发布被本地 evidence gate 拦截。V2 ingest 使用 v2-{run_key}，sync_sources 的临时来源保护却依赖旧 topic_requests code。要修生命周期映射，不是粗暴启用全部来源或绕过证据检查。
2. 草稿 #29 的两次尝试返回桥接 HTTP 500；最新容器日志显示图片上传、标题和正文检查完成，但点击后未确认跳转，请求耗时恰好 2 分钟。这支持调查全链路 deadline 耗尽，不足以认定内容违规/风控。
3. 当前适配器中“5xx + 短暂查询 feed 无匹配 → PublishFailed”的逻辑不可靠。短时间没查到笔记不能证明未发布，必须修回 unknown，除非取得明确拒绝或未发送证据。
4. 最近部分 failed 来自人工确认未发布，部分来自上述过强推断。历史记录不批量改写、不自动重发；按证据区分，导入恢复任务需显式操作。

## 2. 范围和完成标准

### 必须实现

- 新批准产生/唤醒唯一发布任务，Agent 主导发布、结果查询、诊断和恢复。
- 同一批准作品无需每次网络恢复都重新审批；只允许原账号、原作品的有效批准。
- 处理三类核心故障：登录失效、图片路径/文件可恢复问题、临时连接失败。
- HTTP 500、超时、回执丢失必须进入准确的结果判定流程。
- 飞书可查询、要求诊断、同意/请求安全重试、报告登录完成、取消；回复关联具体任务。
- 重启/重复回调/并发 worker 不产生双发布；同一任务保存所有尝试与诊断历史。
- 真实状态由程序和工具证据决定，模型不能自行写 published 或“确定未发布”。
- 持久等待、有限自动恢复、去重通知、模型费用与工具调用计量。

### 不做

- 不改成多 Agent 团队，不训练模型，不新增平台、评论、点赞。
- 不让运行中的 Agent 修改源码、运行任意 shell、关闭系统防护或绕过验证码/平台限制。
- 不以延长 timeout 或删除门禁作为全部修复。
- 不要求跨系统 exactly-once；本地确保单活发送，远端不明确就对账/等待。

验收后产品表述：具备人工审批、持久任务、可观察诊断和受控恢复的小红书发布 Agent，而不是“所有故障自动修好”。

## 3. 架构：一个业务任务，创作与发布分阶段

采用轻量 publication_agent 包，不在旧 publish.py、worker.py 内堆一个巨型循环。它是同一运营任务的发布阶段，不是独立角色聊天团队。

建议目录（允许合理调整，但不得复制三套相同逻辑）：

bot/services/publication_agent/
  contracts.py       Task/Decision/Observation/Error/RecoveryPlan 契约
  store.py           状态、步骤、审批关联、CAS/lease、等待和证据引用
  entry.py           批准接续、飞书命令、调度恢复入口
  runner.py          有界模型工具循环，结束一轮不等于结束业务任务
  tools.py           受控工具定义与 schema
  policy.py          重试资格、取消、批准期限、诊断权限
  diagnostics.py     脱敏日志/页面诊断归一化

继续复用：publish.py 为唯一执行提交的闸门，publishers.py 做协议适配，reconcile_readonly.py 做证据判定，notifications.py 做 outbox。

链路：
Creator artifact → owner approval → publication task → decision/tool loop
→ publish gate → HTTP bridge → structured observation → reconcile/recover/wait
→ 飞书反馈 → 同一 task 唤醒 → 最终 published 或 cancelled。

Agent 不直接拿原始 publish HTTP 工具；它调用 publish_approved_artifact(task_id, expected_generation)，由代码读取冻结包并核验。发布工具没有可自由指定的正文、账号、URL 参数。

现有 Creator run 可以继续标 completed（含义仅为创作完成）。对用户显示的整体任务必须关联 publication task，区分“图文已完成，等待批准”和“笔记已发布”，不能把创作 completed 当作运营任务完成。

## 4. 任务状态与持久化

建议业务状态：
- ready：有效批准已绑定，待处理。
- diagnosing：正在检查，尚未决定提交。
- submitting：已持久占用一次发送资格，请求可能在途。
- verifying：已提交，正在核实结果。
- retry_wait：明确可重试且有 next_wake_at。
- waiting_login：等待用户扫码或完成登录。
- waiting_owner：需要补充判断、恢复授权或人工协助。
- waiting_approval：作品改版或批准到期，等待明确批准当前包。
- paused：用户/全局暂停，非终态。
- published：有可核实成功证据，终态。
- cancelled：用户取消后已无在途/未知发布，终态。

cancel_requested 必须是独立持久字段：用户取消时若请求已在途或 unknown，只停止未来提交，继续只读对账。不能声称已撤回网页操作。若后来确认发布，最终 published 并注明用户取消到达时请求已发送；不自动删帖。unknown 不可被 cancelled 掩盖。

模型单轮失败/超步数不是业务终态 failed。写入阻塞原因，转 waiting_owner 或 retry_wait，通知用户；不能静默丢掉任务。未知故障也不永久“假装还在修”：清楚显示等待人工开发修复。

建议 publication_tasks 字段：id/code、account_id、draft_id/version、creator_run_key、approval_id、approved_package_hash、generation、state、cancel_requested、next_wake_at、wait_reason、active_attempt_id、last_error_class、retry counters、lease/fencing、created/updated、terminal_evidence_ref。

publication_agent_steps 保存 action/args、简短 intent、依据 observation/evidence ID、状态、开始结束时间、工具结果摘要及完整脱敏证据位置。无需保存模型完整内部思维链。

publication_owner_requests 保存问题编号、任务/版本、问题类型、状态、有效期、回复入站 message_id，防止重复或过期回答唤醒错误任务。

使用单独 approved artifact/package 身份锁定同一作品：不要只按 approval_id 防重，因为重新审批可产生新编号但仍是同一稿。发布 attempt 扩展 task_id、attempt_no、dispatch certainty、重试依据、quota reservation。历史行保留兼容。

所有重要状态切换使用事务与 expected generation/CAS；数据库 lease 过期仅代表本地失联，不代表远端请求没有发送。

## 5. 批准、作品不可变与取消

冻结发布包包含账号、平台、draft version、标题、正文、tags、有序图片 ID/hash、可见性与其他影响发布语义的字段。传输用绝对路径可调整，但字节、顺序和内容 hash 不可变。

发布工具只从数据库读取这份批准包，模型不传正文。若发现当前包不同，转 waiting_approval，不覆盖旧批准。

允许不重新审批的动作：同字节图片从已核验备份恢复、受限目录路径转换、连接恢复、确认登录仍为原账号、原任务合法时间内重试。

必须重新批准：改标题/正文/标签/图片内容或顺序/目标账号/可见性；现有批准到期；超过原本明确同意的发布时间窗口。原审批当前绑定 scheduled_at，因此 next_wake_at 只用于任务唤醒，不得暗改 scheduled_at 绕过指纹校验。必要时走明确的重新批准/续期流程。

取消与审批/提交并发必须在最后提交事务复检。已经 published 的取消命令只返回已发布事实，不删帖。不把“暂停”当“拒绝”，也不把普通聊天里的负面措辞当作取消命令。

## 6. 多次尝试与结果分类：先修发布执行边界

旧规则“一条 approval 只能有一个 attempt”需改为：一个 publication task 可有多个有证据支持的 attempt，但最多一个可能在途/未确认 attempt。不要通过删行、清状态、伪造新 approval 绕过原唯一键。

每次提交前：
1. 确认有效批准、账号、包 hash、来源、配额、暂停/取消、generation。
2. 检查无 published 成功记录，无 in-flight/unknown 未解决尝试。
3. 检查重试依据和 cooldown，事务领取发送资格并保存新 attempt。
4. 网络请求在事务外执行；结果落库和 outbox 通知保持一致。

结构化结果至少包括：
status、error_class、stage、dispatch_state、retryable、evidence_refs、external_post_id/url、owner_action、elapsed_seconds、request_correlation_id。

分类：
- not_dispatched：确知 POST 未发（如本地文件预检失败、连接建立前明确拒绝）。修复后可重试。
- rejected：平台或桥接明确返回可验证的未接受结果；需判别是否可修。不能以 HTTP 500 一概判定。
- accepted_unconfirmed / unknown：发送后超时、断连、5xx、进程中断、返回 2xx 无有效回执、取消时仍在途。先只读对账，禁止直接再发。
- published：可靠回执或强唯一的账号笔记证据。

成功判定：同账号、发布前快照、新增 ID、合理时间范围、正文/图片等多项可用信号。只有同标题不够；候选歧义保持 unknown。页面跳转/HTTP 200/模型判断都不是单独成功依据。不要因平台展示截断或字段缺失伪造匹配。

“feed 为空/短时间没出现”不是未发布证明。owner 可通过显式“确认未发布并重试”提供人工对账依据；执行前仍尽可能复查新回执/笔记。若已有成功证据则拒绝重试，即使用户的确认更晚到达。用户泛泛说“重试”不等于确认未发布。

配额需拆分：作品发布名额和请求尝试限额。同一作品安全重试不重复消耗五个名额；unknown 保留占位，成功只计一次；另设尝试次数/频率约束。保留现有手动/自动配额差异和配置值，不擅自提高。跨日占位和实际发布归属以配置时区明确记录并测试，不通过改日期绕过配额。

## 7. Agent 工具与决策提示词

不暴露整个桥接服务，只注册本任务需要的工具：

- inspect_task：读取当前包摘要、批准状态、历史尝试、取消/暂停和允许动作。
- check_bridge_health：只读检查服务可用性、版本、健康与阶段指标。
- check_login：确认登录及实际账号，不输出 Cookie/token。
- verify_assets：检查最终文件、hash、图片可解码、容器映射与目录边界。
- restore_approved_assets：只从该作品的批准资产库恢复相同字节，目标受限且原子写入。
- inspect_failure：读取本任务脱敏错误、页面校验提示、阶段耗时、诊断截图引用。
- publish_approved_artifact：仅由闸门创建并执行一次有资格的 attempt。
- check_publish_result：只读回执/feed 对账，不发送作品。
- request_owner_action：请求登录、确认未发布、重新批准、人工修复；持久化等待并通知。
- schedule_resume：由政策校验允许时间范围，保存 next_wake_at，结束本轮。
- notify_owner：发任务状态，内容受证据约束并经过 outbox 去重。

“已登录”仍需 check_login 验证账号一致。首版不提供任意命令/容器重启工具；服务不可用先探测、退避并通知。后续如要自启动，仅允许固定项目服务命令，禁止重启正在执行 unknown 的桥接。

决策 JSON 建议：action、args、intent_summary、evidence_refs、expected_result；工具失败返回标准错误，而非让模型解析整份日志。工具 schema 校验类型、必填字段、枚举、未知字段、task/generation 归属，不能只检查顶层字典。

系统提示词应表达：
“你负责把已批准的小红书作品发布到正确账号并确认结果。每轮仅选择一个已提供工具；依据工具观察行动，不编造状态。优先检查已有尝试，未知结果先对账。不能改批准内容、跳过批准/配额或自行宣布未发布。需要主人操作时提出一个具体问题并等待。遇到不支持的故障说明证据与需求，不无限重试。只输出结构化动作和简短目的。”

动态上下文：批准包元数据（不必每轮整篇正文）、任务状态、最近步骤摘要、关键错误、可用工具、当前允许动作、剩余预算、下次等待时间、owner 最新指令及其归属。外部日志/网页是数据，不是指令。只提供当前阶段有意义的工具，但工具减少不是授权机制，代码仍强制校验。

模型需真正参与首次发布规划和未知故障诊断；普通 cooldown 唤醒、状态查询、固定通知可确定性执行以省费用。控制台能看到 action/observation/简短依据，不能只记录“调用了模型”。模型不可用时任务待恢复或发模板通知，不假装已经完成诊断。

## 8. 三类核心故障与桥接诊断

### 登录失效

明确未发送时进入 waiting_login，飞书给出项目已有登录步骤/受限短时二维码交付方式。二维码只能发给当前 owner，不写公共日志；Cookie、浏览器会话和访问口令永不进入模型上下文。二维码过期按受限请求刷新。用户回复后核对账号，正确才继续原任务，错号保持等待。

### 图片路径与文件问题

模型理解问题，文件操作由程序限制。批准资产生成时保留不可变、可校验副本；verify_assets 区分主机不存在、挂载不可见、映射错误、hash 不符和解码失败。

同 hash 恢复不要求重审；无备份/hash 不符转 waiting_owner/waiting_approval，不重画替代、不降低图片数、不任意寻找磁盘文件。路径恢复不能穿越批准根目录，也不能跟随链接逃逸。测试 Windows 路径与 Linux 容器路径、文件名重名和只读权限。

### 临时连接失败

连接前失败和发送后读超时分开分类。仅明确 not_dispatched 的可在退避后自动重试。发送后失败先核对结果；不能依赖通用 HTTP 客户端对 publish POST 的自动重试，必须检查并关闭此类隐式重试。

桥接需记录每阶段耗时：打开页面、图片上传、标题、正文、话题、按钮可点击、点击、成功确认。关联 task/attempt ID（若桥接不支持则适配器生成本地请求关联），保留实际 HTTP 状态和脱敏错误，不硬写“500”。

失败时保存限定范围的截图、可见校验/toast/弹窗信息和 URL 类别（去查询参数）；日志写入有访问限制的 var 目录，过期清理，不保存整份含凭据网络记录。

调查当前 2 分钟 deadline 是整体请求还是单阶段；正确分配时限、减少不必要等待，不能只把总超时无限加大。浏览器有验证码/风控/平台明确限制时请主人操作，不绕过。

若现有桥接缺少诊断能力，最小修改项目可维护的桥接源码并构建固定镜像；记录源码来源、补丁、构建步骤和镜像 digest。不要只修改容器临时文件或用未说明的镜像替换生产。HTTP 调用方案保持，无需本次迁移 MCP。

## 9. 飞书交互：自然意图与明确作用对象

支持规范命令，并可为自然语言增加模型意图解析。示例：
- 发布状态 XH-XXXXXX
- 检查原因 XH-XXXXXX
- 修复并重试 XH-XXXXXX
- 已登录 XH-XXXXXX
- 确认未发布并重试 XH-XXXXXX
- 暂停发布 XH-XXXXXX / 继续发布 XH-XXXXXX
- 取消发布 XH-XXXXXX（拒绝发布可为同义命令）

自然语言“帮我修一下再发”“我登录好了”优先从回复消息关联；无回复关联且当前只有一个待处理任务可以解析并回显目标。有多个候选必须询问，不猜最近任务。旧审批码映射到明确作品/任务，过期回答不能影响新版本。

飞书回调只鉴权、去重、持久记录/入队并快速返回，不在回调中跑模型/浏览器。采用 inbox message_id 唯一键 + task/generation，重复点击/消息不重复执行。owner/p2p 检查沿用。

拒绝/取消状态识别要覆盖“已批准但未提交”“等待恢复”“在途”“未知结果”。现有 pending-only 拒绝分支不足以覆盖新生命周期，不能只添加一条字符串别名。

通知：首次开始、状态重要变化、需要主人、最终结果。重试风暴不反复发送同样错误。未知时明确“尚未确认是否发布”，别通知“失败，可以重发”。成功通知包含可核实的笔记链接/ID；模拟成功标 simulated，绝不伪装真实发布。

等待类通知说明：发生了什么、已检查什么、下一步由谁做、用户可回复的具体命令。模型总结需校验状态；异常时使用确定性模板兜底。outbox 自身失败独立重试，不能导致作品再次发布。

## 10. 持续运行与成本默认值

新增 publication_agent 配置，默认关闭；不改当前模型凭据和日费用预算。建议初值（可据实调整并写报告）：
- 每轮 max_steps=6，max_model_attempts=6，最多 180 秒；预算包括重试且请求前持久预留。
- 单任务自动发送尝试上限 3，每日自动发送尝试上限 3；尝试号不因重启重置。
- 明确未发送的连接故障退避 30 秒、2 分钟、10 分钟，可加 jitter；不要 sleep 占用 worker。
- unknown 对账 30 秒、2 分钟、10 分钟，之后等待 owner；不得把有限查询无结果当失败。
- 每日模型尝试上限建议 12/任务，达到后 waiting_owner，不删除任务。用户显式继续可以授予新的恢复批次，但不能清除历史消耗或绕过全局预算。
- 任务跨天存在不代表每天自动重新发一轮。安全前提和原批准有效期仍要满足。

来源禁用、配额用尽、批准到期分别通知，能确定下一可执行时间才调度；内容旧了或有效期过了请求重新批准，不默默跨日发布新闻。

单账号发布串行，检查桥接并发浏览器行为；发布 lease 续租与 fence 防止旧 worker 恢复后重复提交。租约丢失后未发送就停，可能已发送则 unknown。重启执行启动恢复，不能根据 jobs retry 直接重新 POST。

## 11. 旧代码重构与迁移规则

- approved 命令的 enqueue publish 改为创建/唤醒 publication_task + publication_agent job；旧 publish job 通过兼容 handler 进入同一闸门，不保留第二个自动发送入口。
- 历史已发布、已取消任务不重新调度；历史 failed/unknown 不批量导入 ready。提供只读清单和明确逐项导入命令，导入 unknown 必须先对账。
- 创建新表/新增列/index，保持旧审批/结果与审计可查询。不得编辑旧 migration 10，不能通过新建空库绕过升级问题。
- 重构 publish.py 的单次 attempt 幂等，不放松包校验、账号检查和来源安全政策。
- 修复 V2 临时来源归属：明确 configured recurring / creator-owned / explicitly disabled；同步配置不能误伤 creator-owned，用户明确禁用仍应阻止发布。升级修复来源状态需依据任务来源关联，不批量 enabled=1。
- 可删除：确认无调用的旧 topic 编排和重复诊断适配；必须 rg 调用、入口、测试和迁移依赖后列清单。不能为减少测试数删除仍需兼容的发布/安全测试。
- 不新建庞大框架，优先已有 SQLite jobs/outbox、HTTP 和模型适配。全局数据库不是模型工具。

## 12. 测试矩阵：须穿透真实应用入口

全部先 FakeLLM + 假桥接 + 临时 DB + 注入时钟完成；不需发真实帖子。

A. 批准 → publication task → 模型决策 → publish tool → gate → 假桥接 → 回执 → 一次成功通知；检查模型确实看到工具结果。
B. 登录失效 → waiting_login → 重启 → owner 已登录 → 身份正确 → 原包发布；错账号/非 owner/群聊不能恢复。
C. 图片路径错误 → 查到相同 hash 备份 → 恢复 → 发布；无备份、hash 错误、穿越路径、同名文件拒绝。
D. 连接前两次失败后成功，尝试计数正确；发布 POST 不发生隐式网络重试。
E. POST 之后超时但实际已发布 → 只读查到强匹配 → published，POST 总数为 1。
F. 500 + feed 空 → unknown，无自动重发；有明确拒绝证据才判 rejected。
G. unknown + owner 确认未发布 → 复查 → 有资格的新 attempt；晚到成功证据优先，禁止第二次发送。
H. 取消在提交前、最后事务前、请求在途、unknown 时的交错：不虚报撤回，不再发送，保留最终实际结果。
I. 任意阶段重启：claim 后/发前、POST 后/落库前、确认后/通知前；一个任务、正确尝试与 outbox，不重置预算。
J. 两个 worker、重复批准、重复飞书恢复消息、lease 过期旧进程继续：最多一次合法在途提交。
K. 修改版/旧审批码/过期登录回复/批准到期/全局暂停/配额跨日：均不绕过门禁。
L. 最后一次模型额度被 decision 用完后不能再触发收费子工具；HTTP 重试也计数，预算不足保持任务。
M. 来源同步后 creator-owned 证据仍有效，用户明确禁用仍拦截；历史来源迁移不放开不相关来源。
N. 审批/发布结果通知部分失败只重发必要消息，不重发帖子；最终回执去重。
O. 模型返回非法动作/错误 task_id/编造 published/日志含提示注入：拒绝并保存真实状态。
P. 升级旧库、二次初始化、回滚演练、旧 publish job 兼容与历史未知状态保留。

所有新测试不只测 helper。至少三条真实 handle_inbound_message → jobs → worker → publication agent → publisher → notifications 路径，含成功、登录恢复、未知结果取消。

新增一套故障注入演示脚本（可命名 scripts/demo_publication_recovery.py），默认离线，输出脱敏 JSONL：动作轨迹、POST 次数、恢复耗时、模型调用与通知数。不可把模拟轨迹放进真实运营记录。

## 13. DSH 执行阶段与交付物

S0 盘点：读本文件与实际代码，记录现有 schema/启动方式/测试命令；确认项目规则，查看未提交差异（若没有 Git 如实记录），不覆盖其他对话修改。先写实施清单和删除候选，不等用户再次确认常规实现选择。

S1 发布基础：修正错误分类、来源生命周期；增加冻结包/多 attempt 身份和最后事务闸门；通过 E/F/H/I/J/M。

S2 Agent 核心：工具、模型循环、状态持久化、预算与等待调度；做到 A/C/D/L/O。

S3 飞书恢复：批准接续、命令/自然意图与目标绑定、通知、登录恢复与取消；做到 B/G/K/N。

S4 集成：旧 job 兼容、故障诊断桥接能力、测试矩阵、全量回归、临时库迁移/回滚演练。修复事件循环资源警告，不沿用旧全量结果。

S5 交付待验收：更新 docs/IMPLEMENTATION_STATUS.md 和 docs/README.md，交付 docs/delivery/PUBLICATION_AGENT_DELIVERY.md，包含改动清单、删除理由、状态模型、接口、准确测试计数/日志、真实与模拟范围、已知限制、部署/回滚命令及配置差异。

用户现在授权的是实现该需求的开发范围；开发期间不为验收发送真实帖子，不重放最近失败稿，不擅自变更 live 服务或重建生产容器。完成可审查代码、离线测试和迁移演练后，提交具体部署方案供用户确认。不要每个普通代码改动都再问用户。

部署方案应包含：停相关写入/worker，备份实际最新 DB/代码/配置（密钥不复制进文档），迁移校验，新功能开关、兼容旧任务、启动健康检查、回滚步骤。不能以此前旧备份替代当前备份，也不能在业务正在写库时覆盖 DB。

手机真实验收安排（部署获准后）：用户选一篇具体最终作品批准；Agent 正常发布并回执。登录恢复和图片修复优先在 dry-run/测试环境故障注入，不能为测试故意破坏生产登录或删原图。真实重发只对用户明确授权且符合对账门禁的任务进行。

## 14. 最终验收口径

只有以下全部具备，才能写“发布恢复 Agent 完成”：
- Agent 对同一批准作品有真实工具决策和结果反馈。
- 审批后发布任务不会因为一次工具异常丢失。
- 三类指定故障有可验证恢复路径与飞书接续。
- 不确定状态不误判失败，不自动重复发帖。
- 用户取消能停止后续发送，对在途结果诚实报告。
- 包、账号、权限、配额、预算与并发门禁保留。
- 离线穿透测试、主项目全量及临时库升级回滚通过。
- 报告区分“本地已实现 / 离线已验证 / 已部署 / 手机真实已验证”；没有真实验收不写已上线稳定运行。

不接受以下替代交付：仅写提示词；固定重试三次冒充 Agent；把所有错误都交给主人且无诊断；无限模型轮询；只发开始通知不发结果；把 200/页面跳转当发布成功；通过放松批准/unknown 规则让测试变绿。
