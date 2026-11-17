# 实现期间审查意见（由协调助手提供）

2026-11-05 +08:00。这些是已生成代码的早期检查，尚未代表最终测试结果。请在本轮实现中处理，不要覆盖产品需求文档。

1. bot/config.py 的价格量纲错误：当前 input_cny_per_million=0.30、output_cny_per_million=1.20 其实来自官方 USD 价格。请将预算、计价、usage 展示和测试统一为明确的 USD 字段与标签（或提供明确可配置汇率并转换，但不静默假定汇率）。使用保守峰时价格计算，真实账单另行核对；未知用量保守预留，重试也须计入预算。
2. config.py 的 BOT_PUBLISHER_URL 三层 env_map 赋值顺序有误。元组 (publisher, xiaohongshu, base_url) 目前会写入 publisher.base_url.xiaohongshu。请用路径的最后一个元素作叶子、前面的元素依次遍历；添加一个真实验证嵌套环境变量覆盖的测试。
3. Secrets.status() 不能在仅有 lark_chat_id 时把 owner_open_id 报告为 set。owner_open_id 用于授权本人，是独立必需项，不能由 chat_id 替代。飞书回调必须同时检查本人身份和 p2p，拒绝群聊和重放。
4. live 模式中 fake LLM/来源必须只能 dry-run，不能存在配置 allow_fake_in_live=true 之后仍可调用真实 publisher 的路径。最好分清 research/live_read 与 live_publish，实际发布必须拒绝所有演示来源和生成器。
5. 小红书社区服务的当前 docs/API.md 支持 AUTH_TOKEN/Bearer 验证：发布适配器应可从本机 secret/env 读取 XHS 服务 token，不得明文写到普通配置或命令参数，不给新闻请求共享此凭据。仅允许明确配置的 loopback 发布服务。
6. 发布内容哈希包括图片“文件内容”而不只是路径。批准后改图、文字、时间/账号都应使批准失效。未知发布结果不能自动重试，已发布必须有真实回执。

新增信息：Harness 的命令行已用项目 dsh-project.patch.yml 接通并返回 DSH_READY，详见 HARNESS_CONTROL.md。你当前桌面任务继续正常实现，不需要另起命令行任务。不要再花时间探测此问题。
