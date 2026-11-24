# 小红书运营智能体

在飞书提出选题、阅读图文、给修改意见和批准发布。创作 Agent 负责取材与迭代作品，发布 Agent 跟进获批版本的提交、结果确认和故障恢复。

这是基于 DeepSeek API 的大模型应用项目：模型选择行动，Python 工具执行操作，SQLite 保存进度，人决定是否发布。开发工具 DeepSeek Harness（DSH）不是机器人运行时的必需服务。

## 日常使用

1. 私聊飞书机器人：**选题 AI 智能体为什么需要记忆，用一个生活例子解释，少用术语。**
2. 收到正文和配图后给意见：**修改 开头改成我的观点，图片只展示机制和例子，保留第二张图。**
3. 满意后发送 **批准 XH-XXXXXX**，允许对应版本真实发布。
4. 等待结果；需要登录或人工判断时，机器人会说明下一步。结果未知时先查平台，不直接重复发帖。

请用实际审批码替换示例编号。普通修改指令默认针对最近一篇可修改作品，多篇并行时先核对对象。定时选题也只生成待审批作品。电脑需要保持开机、不休眠，后台才能持续处理。

## 系统怎么工作

~~~mermaid
flowchart TD
    A[飞书选题或修改] --> Q[SQLite 任务队列]
    B[定时发现热点] --> Q
    Q --> W[Worker]
    W --> C[创作 Agent]
    C --> T[搜索阅读 / 写稿修改 / 渲染检查]
    T --> C
    C --> R[本人审批具体版本]
    R -->|修改| Q
    R -->|批准| P[发布 Agent]
    P --> G[批准版本、账号、图片与配额校验]
    G --> H[HTTP 适配器 → 小红书桥接]
    H --> E[保存发送记录 / 未知结果对账]
    E --> N[飞书回执或请求主人处理]
~~~

两个 Agent 是不同职责的模型工具循环，不需要两个模型部署。每轮模型读取目标和观察，选择一个工具，程序校验、执行并保存结果，再进入下一轮。

- **创作 Agent**：发现选题、搜索、阅读来源、写稿、按意见修改、渲染图片、审阅、请求补充、提交审批。内部产物与正式审批分开。
- **发布 Agent**：只处理获批版本，选择检查、恢复原图、提交、查询结果或请求主人处理。发布权限与批准边界由程序守住。
- **确定性程序**：处理消息去重、计时重试、状态回复和平台证据核验。自动对账无需每次询问模型。

本地运行三个服务：gateway 接收飞书长连接事件；worker 执行任务和发送通知；console 提供本地审批与维护页面。SQLite 保存任务、作品版本、审批、发送尝试和通知，图片等文件保存在本机目录。

## 技术栈与平台接入

- Python 3.12；固定依赖见 [requirements.txt](requirements.txt)。
- DeepSeek API：结构化行动决策、正文生成与修改；审稿同时有程序检查，是否额外调用独立语义审稿取决于配置。
- SQLite：本机持久队列、检查点和业务记录。
- Pillow：图片渲染与程序化布局检查，不等同于扩散模型生图。
- lark-oapi：飞书机器人接入。
- 社区小红书桥接：应用通过自己的 HTTP 适配器调用所需能力，没有把桥接的所有 MCP 工具开放给模型。

## 快速开始：离线体验

在 Windows PowerShell 中进入仓库目录，需要已安装 Python 3.12。

~~~powershell
.\setup-bot.cmd
.\.venv\Scripts\python.exe -m bot demo
~~~

安装脚本创建虚拟环境并安装依赖。基础 demo 使用本地来源、fake 模型和 mock 发布，输出到 var/demo，结果标记为 simulated，不是真实平台发布。它保留早期基础流水线，用于理解采集、生成、审批和持久化；不是新版双 Agent 的完整演示。

新版发布与故障恢复可用离线集成测试体验：

~~~powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_publication_round2_demo
.\.venv\Scripts\python.exe -B -m unittest tests.test_publication_silence_fix
~~~

这些测试使用模拟外部平台、模型或通知器，验证实际 SQLite 和执行链，不需要真实发帖。

## 配置真实账号

1. 参考 [配置模板](config/bot.example.toml) 创建本机配置，例如 config/bot.live.toml。模板面向真实运行，不要当作离线配置直接启用。
2. 按 [飞书指南](docs/FEISHU_SETUP.md) 配置机器人、本人标识与消息事件。
3. 按 [桥接指南](docs/XHS_BRIDGE_SETUP.md) 和 [容器方案](docs/XHS_CONTAINER_PLAN.md) 配置本机桥接、认证、图片映射和扫码登录。
4. 配置 DeepSeek、来源和通知。检查数据库兼容与创作/发布开关，再启动服务。
5. 用一篇新批准的作品确认平台笔记和手机回执，再扩大使用。

真实发布需要 live 模式、xiaohongshu provider、live_enabled、真实来源与模型、运行环境授权，以及本人对最终版本的批准。仅打开 publication_agent 不能替代这些条件。

密钥只放本机环境变量或 config/secrets.toml。真实配置、数据库、图片产物、日志和虚拟环境由 .gitignore 排除，不上传仓库。

## 启动与维护

已有配置的本机实例可以使用现有脚本：

~~~powershell
.\scripts\start.ps1 -Config config/bot.live.toml -AllowRealPublishDaemon
.\scripts\stop.ps1
~~~

启动支持 -GatewayOnly、-WorkerOnly、-ConsoleOnly；停止支持 -Name gateway、worker 或 console。脚本核验进程身份，避免误停其他 Python 程序。

本地控制台默认地址为 http://127.0.0.1:8765，以配置为准，访问需要登录。电脑休眠或关闭时后台不能继续工作。

**doctor 会调用数据库初始化，可能应用迁移，不能当成严格只读查询。** 迁移前停止写入、备份并验证副本。有新增审批或发送后，不要用旧库覆盖；见 [回滚说明](docs/delivery/PUBLICATION_AGENT_ROLLBACK.md)。服务更新时不要中断正在提交的发布任务。

## 飞书命令速查

只接受绑定的本人私聊。随时发送 **帮助 / 菜单 / help** 查看完整说明。帮助和发布状态回复不消耗模型调用。

**创作与审批**

- 选题 <主题或链接> / 写一篇 <要求>：开始创作。
- 修改 <具体意见>：修改最近一篇可修改作品，收到新版后再审阅。
- 批准 XH-XXXXXX：同意真实发布具体版本。
- 拒绝 XH-XXXXXX：拒绝该审批。

**发布与恢复**

- 发布状态 XH-XXXXXX：查看进度、提交情况和下一步。
- 检查原因 XH-XXXXXX：只读诊断，不触发重新发布。
- 修复并重试 XH-XXXXXX：继续处理；未知结果先查原发送。
- 已登录 XH-XXXXXX：扫码完成后接续指定作品，并重新核验账号。编号标识作品，不是登录凭据。
- 暂停发布 XH-XXXXXX / 继续发布 XH-XXXXXX：暂停或恢复任务。
- 取消发布 XH-XXXXXX：停止后续提交，不能撤回已在途或已发布的帖子。
- 确认未发布并重试 XH-XXXXXX：仅在本人核实平台确实没有发布后使用；在途请求仍受限制。

## 如何处理失败和重启

业务任务与发送尝试分开保存。“把这篇发出去”是一项任务，真正向平台提交一次是一条发送记录，查询结果不算再次提交。

批准绑定具体版本和图片。缺图时只能恢复已批准的相同字节，不能偷偷重画。超时或缺少可信回执时保留 unknown，自动安排有限次数的只读核对；依据目标账号和属于本次发送的证据确认结果，仍无法确认时通知主人。

模型步骤耗尽、预算不足或重复检查无进展时，保存状态并发送停止说明。飞书状态查询直接读库回复。通知失败时补通知，不重新发作品。持久化和对账降低重复风险，但不宣称外部平台具有分布式“恰好一次”保证。

## 代码导航

~~~text
bot/
  lark_gateway.py              飞书入站
  worker.py / jobs.py           后台执行与持久队列
  services/
    creator_agent/             创作决策、工具、版本与检查点
    publication_agent/         发布决策、诊断、恢复与对账
    approvals.py               审批和飞书命令
    publish.py                 发布校验与发送记录
    notifications.py           持久通知和发送
  adapters/                    模型、来源、通知、小红书接口
  schema.py / db.py             数据结构与数据库访问
scripts/                       安装、启停、备份与演示
tests/                         单元及集成回归
docs/                          架构、使用、交付与历史设计
config/bot.example.toml         无密钥配置模板
~~~

初学者先读 [架构学习指南](docs/PROJECT_ARCHITECTURE_WALKTHROUGH.md)，再沿“一条飞书消息 → 入队 → 模型选择工具 → 审批 → 发布记录”追代码。这个项目属于大模型应用工程，不是自行训练或微调模型。

## 验证记录与边界

截至 2026-11-19，现有本机实例的新版发布 Agent 已有真实成功发布记录。随后修复的“停止无通知、发布状态无回复”已有离线回归，独立复跑 18 项通过，线上状态回复也已有发送记录。见 [静默修复交付](docs/delivery/PUBLICATION_SILENCE_FIX.md)：该报告记录全量 374 项通过。这是当次交付记录，不代表后续修改自动通过测试。

正常发布成功不代表所有异常都已做过真机验证。故障恢复主要由模拟平台测试覆盖；自然语言无编号定位、多作品消歧、真实视觉审图、评论互动和完整运营指标分析不属于已验收能力。内容质量仍需要本人阅读和判断。

~~~powershell
# 修改后先做相关定向回归，必要时运行全量
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
~~~

深入阅读：[创作架构](docs/CREATOR_AGENT_V2_ARCHITECTURE.md)、[发布架构](docs/PUBLICATION_AGENT_ARCHITECTURE.md)、[发布恢复交付](docs/delivery/PUBLICATION_AGENT_ROUND2_DELIVERY.md)。历史文档中的“未部署”、测试数量和 PID 属于当时记录，不能覆盖当前运行事实。
