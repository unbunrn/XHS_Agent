# S1–S3 来源作用域最终复核

日期：2026-11-11（Asia/Shanghai）。结论：**本轮限定的来源作用域返修通过离线复核；未发现需继续返修的阻塞项。可进入部署前检查与受控真实验收。** 这不代表已部署或真实 provider 验证通过。

## 独立证据

复跑原反例 `var/tests/parent_r1r4_followup.py`，日志 `var/tests/parent-scope-probe.log`：

- 假站警告：verified=false。
- 同 GitHub 主机不同 owner：verified=false（原漏洞关闭）。
- 官网实际链接的仓库：verified=true（没有以全拒绝代替修复）。
- 身份预算耗尽：异常透传。
- 编排预算耗尽：deferred。

独立执行：

`python -m unittest tests.test_source_scope tests.test_research_r1_r4 tests.test_research_s2 -q -b`

结果：**33 tests，66.432s，OK**。日志 `var/tests/parent-scope-review.log`。

覆盖作用域、前缀/编码路径、缓存重验、策略 fingerprint、正确仓库生成证据和草稿、不同 owner 不形成 verified evidence，以及上一轮 R1–R4 相关回归。代码检查确认首次核验与缓存恢复都调用同一 _link_grant，策略版本参与缓存键。

交付方自报 263 项全套通过，见 S1-S3-research-delivery.md；本轮没有重复全套，不能将其写成独立运行结果。

## 实际能力边界

GitHub、Bitbucket、Gitee 使用 owner/repo 范围；其他站点采用 exact resource 保守匹配。复杂官方跳转、其他共享平台组织结构或官网仅链接首页时，仍可能拒绝真实来源，属于当前召回限制，不应承诺任意新域名都会自动通过。

本轮只修改验收文档、执行离线测试，没有修改业务代码、读取生产凭据/数据库、调用收费 API、发送消息、发布或重启服务。实际运行进程是否加载新代码本轮未确认。离线通过不代表外部搜索质量、真实飞书或小红书行为已验收。

## 下一步

无需再交给 DSH 继续泛化开发。进入部署时，先确认运行版本和任务状态，备份生产库与可回滚代码，演练实际旧版至目标 schema 的迁移，再受控重启。随后通过飞书进行一条研究/解读到审批稿的真实验收；发帖继续保留逐篇批准。部署与真实调用应按用户授权执行，不因本报告自动启动。

所有相对路径均位于本机 `D:\Dev\Projects\bot`。历史问题报告保留，其来源作用域待修状态由本报告更新。
