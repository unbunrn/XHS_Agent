# 调研结论与技术取舍
调研日期：2026-11-05（Asia/Shanghai）。以下“支持”指项目文档声明；未在用户真实账号验证的能力明确保留待测。本文是实现参考，不是已完成报告。

## 对需求的理解
这是一个持续运营单个真实账号的系统：持续监听信息、判断哪些值得说、积累账号记忆、形成原创表达、制作作品、向本人请求必要确认、执行获准发布、收集结果并改进。持久化至少包括任务状态、运营记忆、审批记录、可恢复执行和进程托管；“让模型无限循环”无法代替这五项。

用户已决定：由助手选择易接入的平台与通知渠道；项目位于本机 D:\Dev\Projects\bot；使用 D:\DeepSeek_Harness。未提供账号定位细节、飞书应用、平台登录授权、每月预算或开机保障，采用可改默认值，不能标为已接通。

## 找到的相关项目
1. [Agent Autoloop](https://github.com/KKKKhazix/agent-autoloop)：与需求最匹配的调度和人类介入层，采用事件触发、落盘等待、会话续接和通知插件；已有 DSH 适配。当前 README 写明仅 macOS/Linux、Node 24，Windows 待支持；DSH 插件源码要求 >=0.2.2。本机 CLI 是 0.2.1-rc.5，不能直接宣称即插即用。借鉴设计或未来经验证接入。
2. [AIHOT 开源引擎](https://github.com/KKKKhazix/AIHOT)：采集、预筛、评分、事件聚类与日报的有用参照。包含示范信源而非线上完整来源名单；可使用 DeepSeek，完整部署有 Node 24/PostgreSQL/Docker 等成本。首版只做必要采集能力，不先运营第二个新闻网站。
3. [LangChain Social Media Agent](https://github.com/langchain-ai/social-media-agent)：URL 到 X/LinkedIn 草稿、人工修改/批准、定时任务，适合参考审批工作流。现成配置引入多个服务，非“只有 DeepSeek”即可无改动使用；也不覆盖国内三平台。
4. [Postiz](https://github.com/gitroomhq/postiz-app)：排期、发布与分析工具，有 API 和 agent 集成，适合作为将来的海外平台发布后端。列出的 TikTok 不能等同于中国抖音；主仓库 AGPL-3.0，若复用代码须保留许可证并单独评估分发要求。当前不把它当成新闻核实引擎。
5. [OpenClaw](https://github.com/openclaw/openclaw)：常驻 gateway、聊天渠道、本地记忆与可换模型。解决通用助手底座，仍需自行定义账号策略和逐篇审批门禁；不为了首版而叠加两套通用 agent。
6. [Hermes Agent](https://github.com/NousResearch/hermes-agent)：长期记忆、定时任务、消息渠道与技能积累，可借鉴运营经验沉淀。宣传中的自我改进不等于自动提高账号表现，不能让它自行修改发布权限。
7. [nanobot](https://github.com/HKUDS/nanobot)：Python、持久会话/记忆、定时任务及多聊天渠道，Windows 安装路径明确。是比自建整个通用 agent 更轻的备选；当前任务仍须单独实现内容审校和发布事务。
8. [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)：文档提供登录、图文发布、搜索、笔记和评论读取等能力，能直接使用本地图片。社区非官方自动化；实测可靠性、账号条款和目标环境需确认。只让受控发布器调用，不能把整个 MCP 工具集暴露给写稿模型。
9. [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：官方开放源码、插件化、桌面和 headless 入口。版本仍在预览且可能不兼容。开发工具和运营服务是两个生命周期，不把用户桌面会话当作数据库或生产发布凭据。
10. 用户提到的“gork bot”可能指 Grok Bot；本轮没有获得足够可读的一手产品细节来确认具体指代，因此不将其功能当成已验证选型依据，也不要求购买 xAI 服务。

## 平台选择
- 第一版选择小红书图文：适合中文受众和 AI 工具实用解读；已有社区连接器，模板 PNG 不需额外生成模型。这是结合用户当前资源的产品判断，不是“保证最好涨粉”的结论。
- X 是官方 API 接入更清晰的备选，文本制作简单，但需开发者接入和单独付费。官方当前公布按量计费，普通创建帖子 $0.015/请求、带 URL $0.200/请求；价格随时可能调整，以控制台为准。因此本轮不擅自购买或绑定账单。[X 计费](https://docs.x.com/x-api/getting-started/pricing)、[发布 API](https://docs.x.com/x-api/posts/create-post)。
- 知乎适合长文，但本轮未验证普通个人账号可直接获用的通用官方发布 API，不能承诺免审核自动发布。
- 抖音的视频制作、媒体处理和接入验证工作量更大，放到图文闭环之后。海外 TikTok 集成不能用来证明抖音已可用。

## 手机审批选择
选择飞书企业自建应用、仅本人私聊、官方 SDK WebSocket 接收消息。长连接不需公网入站域名，适合本机运行。
[官方 Node SDK 长连接说明](https://github.com/larksuite/node-sdk#subscribing-to-events-using-long-connection-mode)写明事件与回调能力存在区别；不能把“可接消息”理解为任意版本都能接按钮回调。第一版采用可验证的文本批准命令，卡片展示完整文字与配图；后续实测按钮回调再升级。
Telegram Bot API 长轮询也可免公网回调，但需要当地网络可达，不能默认为用户已经可用。[Telegram getUpdates](https://core.telegram.org/bots/api#getupdates)。
手机确认应显示：平台与账号、正文和所有图片、事实来源、观点与不确定点、预计发布时间、有效期；批准绑定最终版本。通知只在待发布、登录异常、预算/安全问题出现时发送，日常过程不刷屏。

## 新闻来源与事实保障
直接官方公告/RSS 为主，其他媒体是发现线索和补充观点。可信度与热度分开：有大量转载不代表事实已被独立证实。
AIHOT 提供匿名只读 API、MCP、RSS，旧 /api/public 已停用，当前是 /api/v1。[Agent 接入](https://aihot.news/agent)。
匿名访问不等于取得其数据的任意再分发权限；线上服务条款对商业对外使用和批量公开再分发设有书面许可要求。默认不持续搬运其精选摘要；开源引擎和示范信源与线上内容服务分别处理。[服务规则](https://aihot.news/terms)、[OpenAPI](https://aihot.news/openapi-v1.json)。
DeepSeek 当前已提供 Search/Fetch 独立 API，使用同一供应商余额，但属于额外按请求计费；可选开启，RSS 方案不依赖它。[Search/Fetch](https://api-docs.deepseek.com/guides/search/)。
每条事实保存短证据片段、原始来源和获取时间；不可只凭模型自信评分。来源撤回或新证据推翻时，未发稿重新审核，已发稿提出更正方案并请用户决定。

## 持久化及授权设计
数据库保存任务、事件、草稿版本、审批和发布尝试；模型负责提建议，代码负责决定是否有资格执行。
审批等待时结束模型调用。定时器只有发现新材料、到达排期、收到批准或需要恢复时唤醒任务。
发布前的原子校验与发布后的对账缺一不可：外部接口超时不能保证“没发出去”，所以必须进入 unknown 并核对，不能宣称 exactly-once。
长期记忆分三层：账号定位/禁区等稳定设定；来源可信度/选题与表现等事实记录；编辑修改理由等经验。经验更新要保留证据和版本，不能自动改权限。
只对确有来源的数据做复盘，明确区分曝光、点赞、收藏、关注；平台拿不到的指标标缺失，不能用点赞推断收入或增长因果。

## 费用与运行方式
DeepSeek 文档当前推荐 deepseek-flash，并支持结构化 JSON/tool calls；Flash 峰时未命中输入 $0.3/百万 tokens、输出 $1.2/百万 tokens，离峰为其一半。这里只是查询时价格，不是实际消费或月费预测。[模型价格](https://api-docs.deepseek.com/quick_start/pricing/)。
用低成本规则预筛，再对少量候选做模型研究与写作；每日预算、最大候选数、重试上限和队列上限均可配置。计费数据来自实际响应，缺失时不得按零计算。
本机必须开机、联网、账户登录有效。睡眠或断电期间无法运营；恢复时续接状态并丢弃过期任务。云主机是以后的部署选项，不自动购买。

## 本轮可核实的本机发现
- D:\Dev\Projects\bot 已存在且最初没有项目文件。
- 桌面自带 CLI 位于 D:\DeepSeek_Harness\resources\runtime\cli\bin\dsh.cmd，版本 0.2.1-rc.5。
- headless 默认路由为 deepseek-official，需要 API Key；桌面配置的默认路由是 deepseek-account。两条路由不会自动回退。
- 命令行诊断：仅选择账户路由不足以接通；旧版 headless 还停用了 credentials、authorization 和 deepseek-account 服务。项目级 dsh-project.patch.yml 恢复这三个官方服务并选择账户路由后，自检真实返回 DSH_READY。未修改全局配置，未导出或复制令牌。
- 已通过桌面 bot 工作区下达实现任务，观察到 Harness 读取实现文档并检查环境；这里只证明任务已启动，最终实现与外部联通另行验收。
- 实际实现、测试和外部账号联通结果应另写到 IMPLEMENTATION_STATUS.md，只有发生且可验证后才填写。


