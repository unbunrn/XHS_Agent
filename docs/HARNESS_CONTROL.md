# DeepSeek Harness 控制说明

适用环境：本机 D:\DeepSeek_Harness，CLI 0.2.1-rc.5；2026-11-05 +08:00 实测。

## 已完成的接通检查
桌面默认使用 deepseek-account，headless 默认使用 deepseek-official，并禁用了交互凭据服务。只切换模型路由会报 ACCOUNT_SIGN_IN_REQUIRED。
项目根目录 dsh-project.patch.yml 仅包含插件启用和模型路由，没有密钥。它恢复已安装的官方 credentials、authorization、deepseek-account 服务，依旧通过 Harness 的正常凭据与目标域名校验。没有复制账户令牌，没有修改桌面 profile。

已实际执行的最小测试：调用本机 dsh.cmd 的 headless，应用项目 patch，要求不使用工具、只回复 DSH_READY。结果：exit code 0，返回 DSH_READY。

## 命令行入口
在项目目录用 PowerShell 调用：

```powershell
& 'D:\DeepSeek_Harness\resources\runtime\cli\bin\dsh.cmd' headless --patch 'D:\Dev\Projects\bot\dsh-project.patch.yml' '读取 docs/IMPLEMENTATION_STATUS.md，总结当前完成项和阻塞项；本轮不要修改文件。'
```

这是正常模型调用，会产生用户账户用量。实现任务应限制到本项目，具体发布操作仍由独立业务审批控制，不得把 Harness 账户连接等同于平台发布授权。

--json 可以输出运行事件；有会话 ID 时可使用 --session-id 续接。只有最后完成回执及文件/测试证据共同支持时才记录完成。不要把“进程启动”或“模型说完成”当作验收。

当前内置版本没有较新文档中的 --permission-mode 和 --approval-policy 参数，不能照搬新版本调用命令。Agent Autoloop 的现有内置适配器要求 >=0.2.2 且目前未支持 Windows；本项目没有安装它。

## 与运营服务的区别
此配置用于指挥 Harness 开发和检查项目。运营服务若直接使用官方 HTTP API，仍需用户在本机配置 DEEPSEEK_API_KEY；桌面登录不等于已经给 Python 服务配置了 API Key。账户登录过期时应由用户在官方界面续登，不能提取 token 绕过登录。

## 已发给桌面 Harness 的任务
bot 工作区中的“实现机器人项目 P0 到 P2”会话已收到 docs/IMPLEMENTATION_BRIEF.md 和 docs/RESEARCH.md 对应的实现指令：完成本机闭环、接入代码和必要测试；禁止真实发帖、修改其他项目、提交/推送或购买服务。尚需根据实际文件与测试结果验收。

## Windows 多行参数截断
已实测：经 dsh.cmd 传多行 prompt 可能只收到第一段。完整任务必须先写入项目文档，headless 只传单行指令要求阅读全文；不要依赖多行命令行参数。主控仍须复核实际变更与约束。

