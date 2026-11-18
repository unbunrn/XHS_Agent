# 文档入口

- [发布恢复 Agent 独立审查（2026-11-18）](delivery/PUBLICATION_AGENT_INDEPENDENT_REVIEW_20261118.md)：原始 46 项回归通过，但独立探针复现暂停仍发帖、未知取消过早终结及恢复预算阻塞；审查结论仍是不通过部署验收。

## 最新开发任务

- [发布恢复 Agent：Round-2 返修与离线故障恢复演示（2026-11-18）](delivery/PUBLICATION_AGENT_ROUND2_DELIVERY.md)：先复现“列表属于谁”和“检查原因改变发布决定”两个反例，再修身份/列表同源核验、诊断只读、预发送基线、unknown 不重置；离线演示覆盖资产缺失→同字节恢复→一次 POST→通知重启→重复事件不重发，以及跨账号同标题不认领、超时 unknown→同事务自动入队→队列退避→重启 worker 认领原 attempt；主人不发消息也能恢复。定向 **77/77**（517.425s）、主全量 **354/354**（943.785s，`var/tests/full_suite_round2_startup_attempt_source.log`）通过；migration 11 仅临时副本演练，**live 未启用、未迁移、未真实发布**。回滚副本演练与 [回滚说明](delivery/PUBLICATION_AGENT_ROLLBACK.md) 已更新。
- [发布与故障恢复 Agent：R1–R6 返修交付报告（2026-11-18）](delivery/PUBLICATION_AGENT_DELIVERY.md)：R1–R6 返修和 handoff 反例；定向 **71/71**、主全量 **333/333** 通过（799.335s）。migration 11 仅在临时副本演练，**live 未启用、未迁移、未真实发布**。补充报告：[handoff 反例返修](delivery/PUBLICATION_AGENT_HANDOFF_DELIVERY.md)。另见[架构](PUBLICATION_AGENT_ARCHITECTURE.md)、[使用](PUBLICATION_AGENT_USAGE.md)、[进度](delivery/PUBLICATION_AGENT_PROGRESS.md)。原始任务书：[DSH 开发任务](DSH_PUBLICATION_RECOVERY_AGENT_TASK.md)。

最新独立检查：[Creator Agent V2 状态与改进优先级](delivery/CREATOR_AGENT_V2_INDEPENDENT_REVIEW_20261113.md)（2026-11-13，20 项离线测试通过，但修改路由、重复审批及事实检查反例已复现；不能视为完整交付）。

**当前新开发任务**：[创作智能体 V2：DSH 改造任务书](DSH_CREATOR_AGENT_V2_TASK.md)（2026-11-12）。核心交付：[统一交付报告](delivery/CREATOR_AGENT_V2_DELIVERY.md)、[架构](CREATOR_AGENT_V2_ARCHITECTURE.md)、[使用](CREATOR_AGENT_V2_USAGE.md)。V2 已部署到主项目并接入 live worker/Feishu/真实审批；2026-11-13 一条 V2 生成、owner 批准的笔记已真实发布并确认回执。V2 代码只保留在主项目；原隔离副本已清理。

当前 DSH 执行入口：[S1 收尾 + S2/S3 一次性交付任务](DSH_RESEARCH_S1_S3_TASK.md)。2026-11-12 新增 AIHot 新闻发现与二次分析流程，详见 [AIHot 发现层](AIHOT_DISCOVERY.md)。同日新增 P0 内容 Agent（审稿、自适应补检、自动补救、离线评估），详见 [P0 内容 Agent](AGENT_LOOP.md)；离线全量 322 项通过；live 审稿已关闭，自适应补检与自动补救保留，worker 已加载。用户已授权连续开发，不再按阶段等待复核。总体架构参考 [开发说明书](DEVELOPMENT_ROADMAP.md)；DSH_NEXT_TASK.md 是历史 P0 任务，不是本轮入口。

## 学习与课程展示

- [项目学习指南](PROJECT_LEARNING_GUIDE.md)：面向初学者的架构、技术栈、数据流、可靠性、源码路线、练习与答辩准备。
- [课程大作业：从工作流到有边界的运营 Agent](COURSE_AGENT_DESIGN.md)：现有 Agent 能力、四组消融实验、指标、微调选型与两周计划。

## 当前问题专项

- [AIHot 新闻发现与二次分析](AIHOT_DISCOVERY.md)：2026-11-12 实现；AIHot 只做发现层，优先抓原文，原文未取到才用明确标注的聚合摘要；审批/发布闸门不变。live 已开启自动匹配；链路测试 `XT-EFKZF9` 生成审批 `XH-DXQC9Z`，随后作为链路测试拒绝；80 项针对性测试、295 项全量离线测试通过。另修复小红书桥接 2xx 无 note id 的误报：发布前快照账号 feed，成功后仅当唯一新增笔记标题完全一致才记 `published`；`test_xhs_identity` 11 项、相关 37 项通过。
- [P0 内容 Agent：审稿、自适应检索、自动补救与离线评估](AGENT_LOOP.md)：2026-11-12 完成；审稿最多一轮重写，自适应补检最多 2 条，写稿因证据不足拒绝时自动补检一次/再抓 2 次；均不越过来源/时效/审批/发布闸门；评估为 replay 指标层，不调用模型；新增 15 项单测、77 项定向通过，P0 全量 322 项通过（778.082s，日志 `var/tests/full_suite_rescue.log`）。live 审稿因 owner 要求关闭（`critic_enabled=false`），自适应补检与自动补救保留；worker 已于 21:40 重启加载（PID 19632）。真实 20-topic 评测尚未运行。
- [选题检索与正文证据改进](RESEARCH_PIPELINE_REPAIR.md)：S1/S2/S3 与 secondary 来源策略；`XH-MUEAA5` 的旧 unknown 已人工对账为未发布并关闭，`XH-8Q9256` 已确认发布。

## 当前优先级

本轮 S1 收尾、S2 研究计划/来源身份、S3 模式与飞书运维命令已部署；live 已启用批准后立即发布、选题上限 10；2026-11-12 新增 AIHot 发现层（原文优先、摘要回退、非官方标签），自动匹配已开启。真实账号、unknown 对账和 AIHot 内容质量仍需单独验收。P1 长期运行恢复延后。

## 交付报告

- [AIHot 新闻发现与二次分析](AIHOT_DISCOVERY.md)（2026-11-12；AIHot feed/原文优先/摘要回退/SSRF 边界/测试与限制）


- [S1–S3 部署记录](delivery/S1-S3-deployment-20261111.md)（2026-11-11，schema 8、三个服务已启动、飞书长连接已恢复；小红书登录失效待重新扫码）

- [S1–S3 来源作用域最终复核](delivery/S1-S3-scope-verified-20261111.md)（33 项独立离线测试通过，原反例关闭；未部署，待真实验收）

- [S1–S3 返修复核](delivery/S1-S3-rereview-20261111.md)（共享托管站机构/路径作用域已返修并补缓存/端到端测试，见交付报告；未部署）

- [S1–S3 独立验收](delivery/S1-S3-independent-review-20261111.md)（2026-11-11，R1–R4 已返修，见交付报告的独立验收返修节；仍暂不部署）

- [S1 收尾 + S2/S3：计划、来源身份、模式与运维命令](delivery/S1-S3-research-delivery.md)（离线实现 + R1–R4 返修 + 共享托管作用域返修 + 第三方 secondary 来源策略；102 项针对性测试 + 266 项全量离线测试通过；迁移 8 合成库演练通过；2026-11-11 17:46 已部署重启服务，尚未用真实飞书消息/发布验收）

- [S1 返修后的独立复核](delivery/S1-rereview-20261110.md)（历史复核：当时仍有两项缺陷；本轮已补修，见 S1-S3 交付报告）

- [S1 独立验收与返修清单](delivery/S1-independent-review-20261110.md)（离线反例已复现，暂不部署）

- [S1 检索管线：诊断、排序与直接链接](delivery/S1-research-pipeline.md)（2026-11-10，独立验收返修完成，离线测试通过；未部署）
- [P0 发布结果闭环](delivery/P0-publish-result-loop.md)（2026-11-10，离线实现与测试；未部署）
- [P0 独立验收与返修清单](delivery/P0-independent-review-20261110.md)（2026-11-10，3项已返修并补回归；未部署）

- [P0 返修复核与部署](delivery/P0-deployment-20261110.md)（24项独立测试通过，已部署，待真实操作验收）

## 按需参考

- 项目根README.md：安装、命令与运行方式；部分早期演示描述不代表当前live配置。
- HARNESS_CONTROL.md：DSH命令行入口与Windows单行参数限制。
- FEISHU_REMOTE_TOPICS.md：手动选题实现。
- EDITORIAL_UPGRADE_RESULT.md：图文独立策划实现和限制。
- CONTAINER_IMAGE_PATHS.md / XHS_BRIDGE_SETUP.md：小红书桥接及图片映射。
- SESSION_HANDOFF_20261108.md：早期至2026-11-10接续证据；只在排查历史问题时读取。

*_TASK.md、*_REVIEW.md、IMPLEMENTATION_STATUS.md及日期验收文件是历史资料，不可默认当作最新需求。实际配置与明确的新用户指令优先；发现矛盾时查代码和证据，不自行编造完成情况。

后续每阶段真实交付报告放docs/delivery/，并在这里新增链接。不要移动、删除历史文档或预先创建虚构交付报告。
