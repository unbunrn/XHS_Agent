# S1–S3 部署记录

日期：2026-11-11（Asia/Shanghai）。用户明确授权直接部署。

## 已完成

- 部署前核对进程归属：原 worker/gateway/console 分别 26924/23728/4744；按项目 stop.ps1 停止。
- 部署前 jobs 为 done=62、failed=19，无运行任务；publish_attempts 无 publishing/unknown，approvals 无 pending。
- 备份目录：`var/live/backups/s123-deploy-20261111-162942`。包含 before.sqlite3、bot.live.toml、迁移演练库、migration-result.json 和当前已审代码 release-code。
- 注意：代码快照是此次部署版本，不是旧进程加载过的历史源码；不声称已有完整上一版源码回滚包。未复制秘密文件。
- 在备份副本演练 schema 5→8，重复初始化成功；生产库随后迁移至 8。两者 integrity_check=ok。
- 迁移前后行数一致：approvals=6、drafts=5、publish_attempts=2、notifications=29、topic_requests=4、jobs=81。
- 按原 live 配置重启，保留 BOT_ALLOW_REAL_PUBLISH=1 与逐篇审批闸门；console PID 9424、worker PID 24072、gateway PID 17764，进程归属检查通过。
- localhost:8765/healthz 返回 HTTP 200、ok=true、real_publish_armed=true、pending_approvals=0、publish_unknown=0。
- 新 worker 日志可见 16:29:53 启动与后续正常 tick；gateway 最新日志段确认长连接建立。历史 worker 日志含旧 Traceback，当前启动末尾无该异常。
- scripts/health.ps1 完成，三个进程运行，配置/数据库等检查通过。

## 当前限制：小红书需要重新登录

部署后只读 publisher 检查返回 logged_in=false、publish_ready=false，桥接报告账号未登录。因此“服务部署成功”不等于“当前可真实发帖”。需要用户本人重新扫码后再检查账号身份；不要重复提交既有已发布作品。

本次没有批准任何草稿，没有主动发送测试消息或执行真实发帖，也没有人工触发新研究。原有后台运营恢复运行，后续仍按既有配置调度。

## 用户验收

现在可在飞书发送 `帮助` 或 `失败原因` 验证新入口，再发送一条 `选题 ...` 或 `分析 ...` 测试真实研究到审批稿。审批图文需由用户核对；真实发布须先恢复小红书登录。

预算仍按原配置 5 美元/200 次每天；发布配额及审批规则未修改。真实搜索召回、真实模型核验质量和新的飞书往返未在此次部署中验证。

## 恢复说明

备份位于本机 `D:\Dev\Projects\bot` 上述目录。出现问题先停止相关服务并备份现库；产生新记录后不可直接以 before.sqlite3 覆盖。数据库恢复需核对部署后变化；旧版代码需另行取得可靠历史来源，不能把本次 release-code 误当旧版。部署期间一次维护脚本因缺少 tzdata 在执行任何备份/迁移前退出，随后用明确 UTC+08:00 固定时区重跑成功，无需修改运行依赖。
