# 发布与故障恢复 Agent：进度

## 2026-11-18：R1–R6 返修

- R1：结构化 `failure_class`/`retryable`；5xx/超时/无回执为 unknown，不自动重发；实际 XHS adapter 500 + feed 晚到笔记经 gate/agent，POST=1。
- R2：claim 事务检查 task 状态、cancel、generation、冻结包 hash、lease owner、活动/未决 attempt；暂停/取消反例 POST=0。
- R3：feed 只读对账；unknown 取消不终结；晚到成功优先；owner 确认绑定 attempt/generation 并 CAS。
- R4：批次/日预算、transport 前持久预留、owner 新批次、event-key 唤醒去重、只读诊断、worker 异常重试。
- R5：不可变资产 manifest、asset_id 恢复、根目录/链接/hash 校验、临时写+原子替换+恢复后 verify。
- R6：V2 来源归属、`work_key` 语义身份与唯一索引、按作品配额、冻结包 title/tags/visibility/有序图片、simulated 分离。
- 定向 publication/xhs 回归：上一轮 64/64 OK（204.120s）；主全量 326/326 OK（645.702s）。migration 11 临时副本演练通过；live 未迁移。
- 文档：返修交付报告、架构、使用、README/状态已更新。

## 2026-11-18：handoff 反例补充返修

- 新增 `tests/test_publication_handoff_recovery.py`：诊断派生 `schedule_resume` 不得越权发布；旧同标题笔记在首次/重启对账都不得认领；新增预发送基线、账号/读取失败/多候选反例，以及飞书入站→worker→实际 adapter 的成功链路。
- 发送前写 `publish_attempts.pre_send_baseline_json`；对账只接受当前账号下基线外唯一新增标题或 exact id。
- 最新定向 **71/71 OK（217.391s）**；主全量 **333/333 OK（799.335s，日志 `var/tests/full_suite_publication_handoff_final.log`）**。migration 11 副本演练通过，新增列存在；live 未迁移、未部署。
- 详见 `docs/delivery/PUBLICATION_AGENT_HANDOFF_DELIVERY.md`。

## 未完成

- 真实飞书/桥接登录/真实发布验收；真实桥接阶段日志/截图/toast/2 分钟 deadline。
- 自然语言/无审批码唯一目标解析；多进程/多重启通知压力。
- 部署与维护窗口回滚演练；`publication_agent.enabled=false` 保持不变。
