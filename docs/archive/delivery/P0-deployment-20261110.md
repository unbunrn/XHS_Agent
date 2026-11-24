# P0 返修复核与部署记录

时间：2026-11-10T19:05:58+08:00

结论：上次三个阻塞问题修正，相关离线验收通过；按用户明确授权部署，等待用户真实操作验收。未主动发帖或发送测试消息。

- 已复核：弱候选必须提供账号与正文证明；活跃publishing不进行自动对账；终态保护与CAS防止旧回调覆盖；worker未配置发布器通过统一结果记录并通知。
- 独立测试：tests.test_publication_results、tests.test_concurrency、tests.test_split_publish_quota、tests.test_publish_quota_recovery，共24项通过，58.933s。日志var/tests/parent-p0-rereview.log。未重复运行DSH报告的全套测试。
- 合成v4旧库升级至v5、重复初始化与完整性通过：var/tests/parent-p0-migration-4de45e11/old.sqlite3。
- 部署前无在途任务或publishing；停止gateway/console时worker PID文件已不存在，未据此推测退出原因。
- 生产备份：var/live/backups/before-p0-deploy-20261110-190459.sqlite3。
- 生产迁移4→5成功，integrity_check=ok；approvals/publish_attempts/drafts/notifications/topic_requests行数保持。
- console/worker/gateway已重新启动。healthz ok=true、real_publish_armed=true、publish_unknown=0、pending_approvals=0；新worker日志有正常tick，gateway日志观察到长连接建立。
- 配置核对：自动1/日、人工5/日、估算费用5美元/日、调用200/日；保持逐篇批准。

限制：真实小红书只读provider尚未实现并验证；无ID/URL的发布可能仍返回unknown并要求用户提供真实链接，绝不自动重发。历史发布记录没有回填为新的publish_results，这不是历史记录丢失。本次没有验证实际飞书结果消息及真实发布，需要用户下一次正常选题、批准后验证。

用户测试：飞书发送选题→审核正文和图片→批准新审批码→检查是否收到明确发布结果。真实已发但回执unknown时提供笔记链接，不重复批准/重复提交。

回滚：先停所有服务并保存部署后的数据库备份；不能在产生新数据后直接覆盖为旧备份。v5为追加表，优先回滚对应应用代码并保留数据；如需数据库恢复，须先审阅部署后新增记录及停机恢复方案。
