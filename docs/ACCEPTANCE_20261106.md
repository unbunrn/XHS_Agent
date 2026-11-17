# 独立验收记录（主控）
记录更新时间：2026-11-06T17:40:19+08:00。更新时间不代表各项完成时间；以下均有本机证据，不含密钥。

## 当前结论
代码实现和基础真实接口已验证，飞书双向通信已打通。完整图文手机审批彩排已完成；小红书账号绑定及真实发布仍未完成。config/bot.toml 仍为离线默认配置，没有开启持续付费采集或真实发布。

## 已验证
- 主控使用普通 .venv 独立运行最终套件：104 项测试，110.711 秒，OK。证据：var/live-verification/final-tests.log。DSH 的 59/78/95 项是历史阶段。
- .venv 已由普通 Python 3.12.14 重建，include-system-site-packages=false；Pillow 12.3.0、lark-oapi 1.7.2 可导入，pip check 通过。旧环境保留为 .venv-dsh-backup-20261106。安装过程使用项目临时目录和 --no-cache-dir，未修改全局环境或 TLS 校验。
- 隔离离线控制台生命周期：var/live-background/acceptance.toml 启动 localhost:18765，HTTP 200；stop 核验进程身份后停止，端口不可连接、子进程退出。该项不等于三个正式后台进程全部验收。
- DeepSeek models、Search、Fetch 真实调用成功。最终正式 DeepSeekLLM 写作适配器（不是临时诊断传输）生成样稿“给AI助手补上搜索能力”及两张 PNG；prompt 3292 / completion 881 / total 4173 tokens。来源发布时间未知，样稿不是今日新闻。证据：scripts/live_verify.py、var/live-verification/report.json、draft.json、draft.md、images。
- 2026-11-06 17:21 +08:00，正式 Search 采集单轮返回 3 篇文章、3 个事件、6 条证据；所有 summary_length=0，无错误；0 个选题达到门槛，没有强行起草。该轮账本 1 次调用，估算 USD 0.00075。证据：collector-report.json、collector-verification.sqlite3。该项不代表持续运营已验收。
- 飞书认证成功，机器人“AI 运营助手”；用户明确授权的连接消息成功送达，用户确认手机收到。证据：feishu-send-test.json。
- 飞书接收问题定位：用户截图显示长连接已选，但已添加事件为空。用户随后报告添加 im.message.receive_v1 并发布新版本。
- 2026-11-06 17:36:55 +08:00，程序实际收到匹配已绑定 owner 的私聊事件：owner_private_message_received，接收进程 exit 0。证据：feishu-receive-test.json；该检查未发送额外消息，未批准或发布作品。
- 小红书社区桥 v2.7.2 的两个 Windows 程序已从项目官方发布页下载并核对 SHA256，位于 var/tools/xiaohongshu-mcp/v2.7.2；只运行帮助参数，未启动服务、未扫码。

## 待完成及边界
- 手机彩排通过：2026-11-06 17:48 +08:00，本人批准 XH-ECZFVJ 已入库，正文及两张图片一次送达，结果回执已发送；用户确认看到正文和两张图。证据：var/feishu-rehearsal-20261106/phone-result.json。publish_attempts=0，仅彩排，不是发布批准。
- 小红书未登录、未核验 redId、未真实发布。缺少帖子 ID/URL 的回执必须保留 publish_unknown，人工核对，不自动重发。
- 未启动全天持续服务、未安装开机启动；本机运行依赖电脑开机联网。
- 内容校验主要覆盖格式、引用、长度和数字，不能代替完整语义事实核查。
- 多平台、评论读取/自动回复、真实运营指标回收尚未实现。
- 用量金额为本地估算，不是最终账单或绝对消费上限。
- 没有社交平台作品发布，没有创建真实作品发布批准，没有 Git 提交或推送。

## 证据目录
var/live-verification 为真实接口、样稿、测试和飞书证据；var/live-background 为隔离后台验收记录。接口依据见 docs/VERIFIED_API_NOTES.md，配置步骤见 docs/FEISHU_SETUP.md。

