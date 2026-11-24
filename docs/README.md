# 文档入口

当前项目主线只有一条：**飞书/定时选题 → Creator Agent 创作 → 本人审批具体版本 → Publication Agent 提交与对账 → 平台回执**。先读根目录 [README](../README.md)，再看下面三份当前文档。

## 先读这三份

1. [根 README：日常使用、启动与命令速查](../README.md)
2. [Creator Agent V2 使用说明](CREATOR_AGENT_V2_USAGE.md)：选题、修改、审批与创作 Agent 的边界。
3. [Publication Agent 使用说明](PUBLICATION_AGENT_USAGE.md)：发布状态、检查原因、修复、unknown 对账与人工确认。

## 需要深入时

- [项目架构走读](PROJECT_ARCHITECTURE_WALKTHROUGH.md)：双循环、SQLite 账本、审批绑定和发布证据。
- [Publication Agent 架构](PUBLICATION_AGENT_ARCHITECTURE.md) / [Creator Agent V2 架构](CREATOR_AGENT_V2_ARCHITECTURE.md)。
- [项目学习指南](PROJECT_LEARNING_GUIDE.md)：面向课程展示和源码阅读。
- [飞书配置](FEISHU_SETUP.md) / [小红书桥接](XHS_BRIDGE_SETUP.md) / [容器与图片路径](CONTAINER_IMAGE_PATHS.md)。
- [发布回滚说明](delivery/PUBLICATION_AGENT_ROLLBACK.md)、[发布静默修复报告](delivery/PUBLICATION_SILENCE_FIX.md)、[创作/修改统一修复报告](delivery/CREATION_REVISION_FIX.md)、[当前交接单](delivery/SESSION_HANDOFF_PUBLICATION.md)、[本轮精简报告](delivery/SIMPLIFICATION.md)。

## 历史资料

已完成的旧任务书、旧轮次审查、旧交付报告和过时状态文档统一移到 [docs/archive](archive/README.md)。它们只用于追溯，**不代表当前需求或当前验收结论**；实际行为以代码、当前配置和根 README 为准。

新增交付报告继续放在 `docs/delivery/`，并在本页“需要深入时”补一条链接。不要再把历史任务写成“当前任务”。
