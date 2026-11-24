# 文档导航

面向开源用户与开发者的核心文档入口：

## 1. 快速上手与环境配置

- [项目总览与快速开始](../README.md)：功能介绍、离线 Demo、真实账号配置与飞书命令速查。
- [飞书机器人配置指南](FEISHU_SETUP.md)：创建飞书自建应用、配置长连接事件与绑定 `owner_open_id`。
- [小红书本机桥接配置](XHS_BRIDGE_SETUP.md)：配置本机/容器小红书桥接服务、鉴权 Token 与登录态维护。
- [容器图片路径映射](CONTAINER_IMAGE_PATHS.md)：Docker 版桥接容器只读挂载 `var/drafts` 的路径映射说明。

## 2. 双 Agent 使用与架构

- [Creator Agent V2 架构](CREATOR_AGENT_V2_ARCHITECTURE.md) / [使用说明](CREATOR_AGENT_V2_USAGE.md)：创作工具循环、6 套 HTML/Edge 视觉主题、人设与去 AI 腔配置。
- [Publication Agent 架构](PUBLICATION_AGENT_ARCHITECTURE.md) / [使用说明](PUBLICATION_AGENT_USAGE.md)：审批指纹绑定、故障恢复与 `unknown` 只读对账机制。
- [Agent 编排循环说明](AGENT_LOOP.md) / [AIHot 热点发现](AIHOT_DISCOVERY.md) / [飞书远程选题](FEISHU_REMOTE_TOPICS.md)

## 3. 代码学习

- [项目架构走读](PROJECT_ARCHITECTURE_WALKTHROUGH.md)
- [项目学习指南](PROJECT_LEARNING_GUIDE.md)
