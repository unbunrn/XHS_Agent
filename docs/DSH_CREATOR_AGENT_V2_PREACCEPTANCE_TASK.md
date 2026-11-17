# V2 第四轮复核与下一步任务：发布前最后一批边界修复

更新时间：2026-11-13T22:43:00+08:00。
环境：当前 Windows 电脑，D:\Dev\Projects\bot。
依据：21:17/21:18 更新的交付报告和进度、当前源码、本轮独立离线验证。

## 结论与已完成范围

本轮有实质进展，不需要推翻现有实现。父稿/材料已进入决策 observation；worker 加载持久材料；migration 10 增加材料表；最终提交使用图片 manifest，不再直接从正文重新渲染；恢复分支的 generation 检查已前置，lineage 回填已增加。

- 独立执行 tests.test_creator_agent_v2、tests.test_creator_agent_live、tests.test_migration_v2：31 项通过，52.346 秒，退出码 0。日志 var/tests/parent-v2-round4.log。
- 检查 DSH 的 var/tests/full_suite_t1_t4_current.log：记录 353 项、808.109 秒、OK，另有 unclosed event loop 的 ResourceWarning。该全量是交付方运行，本轮没有重跑全量。
- 独立故障探针 var/tests/parent_v2_round4_probe.py / parent-v2-round4-probe.log：三个失败行为见下文。临时库、FakeLLM，不调用外部服务。
- migration 10 未部署是交付方报告，本轮未操作 live 数据库、重启 worker 或改变配置。

现阶段还不能宣布第一批全部完成。下一批只做 N1–N4 与验收收尾，不做新平台、多智能体重构或偏好学习。旧发布闸门保留。

## N1（P1）：草稿创建与 artifact 关联之间仍有恢复缺口

位置：live.py submit_for_approval 中 _insert_draft → artifact.draft_id → store.save_artifact。

独立复现：让 save_artifact 在 draft 创建后抛出异常，重新从数据库读取 artifact 后重试。持久 artifact.draft_id=null，drafts=1，重试抛 UNIQUE constraint failed: drafts.topic_request_id。

与上一轮区别：已保存 draft 指针后的恢复已经改善，但指针尚未保存这一更早窗口仍不可恢复。不能用只测 finalize 前失败来声称全部崩溃窗口覆盖。

实现：为 run/作品版本建立持久唯一 submission key；将 draft 创建与提交关联原子写入，或通过唯一 key 找回已创建 draft 并接续。不能只在内存保存指针。manual、revision、scheduled 都应适用，不能仅依赖手动 topic_request_id 的唯一约束。已有 approval 时还需幂等补全 lineage，不应提前返回而留下关联缺失。

验收：在 draft 插入、指针保存、manifest 注册、approval/通知事务、lineage 回填前后分别注入失败，用新的 runtime/worker 恢复。每个作品版本最终只能有一个 draft 和有效审批、一套逻辑通知任务、完整关联。测试 approval/通知事务回滚，而不是只删除成功审批伪造全部崩溃。

## N2（P1）：取消/代际校验须覆盖最终审批事务

位置：live.py submit_for_approval:575 起；drafts.py _finalize_with_approval。

独立复现：将 run.status 改为 cancelled，保留相同 generation，再 submit，返回 ok=true，approvals=1。当前只比较 generation，没有检查取消状态。

当前所谓 generation CAS 仍是 store.get_run 后普通比较，未与最终创建审批在同一事务内比较并提交；校验后、提交前发生取消或修订仍存在竞态。直接 helper 探针不证明生产中已经触发竞态，但足以证明最终闸门缺少保护；需增加真实 worker 并发/交错测试。

实现：

- 最终审批事务中核验 run 的账号、状态、generation、作品 run_key/version 和提交包 hash；拒绝取消、过期或被替代任务的新审批。
- 对同一有效版本的重试允许幂等读取已有结果，不能为实现门禁破坏 completed 的正常恢复。
- 提交中途取消/更新 generation 时，外部文件准备可以完成，但最终数据库提交必须失败；不能新建有效审批和通知。

验收：cancelled 探针 approvals=0；在预检后、finalize 前切换 generation 或取消，仍拒绝审批；有效代重试仍幂等。不要仅测提交前修改 generation。

## N3（P1）：图片保留是硬约束，不能静默降级

位置：live.py render_images:348 起。

独立复现：将已标 preserved 的图片路径指向不存在文件，render_images 成功返回，preserved_flag=false。因为 source.is_file() 为 false 时直接使用新渲染图片。另一个源码边界：新 renderer 返回的页数少于保留编号时，循环不会访问该保留资产，会直接丢失。

实现：

- 渲染前校验要求保留资产存在且 hash 与父版本一致；缺失或被篡改时，恢复原资产或给出明确可行动错误，不得静默重画、删图或重新计算 hash 接受变更。
- 渲染后校验所有要求保留的资产均在最终 manifest 中；新故事板缩减页数时，保留或明确请用户决定，不能忽略要求。
- 最终文件命名按资产 ID/ordinal 区分，避免多个不同源文件同名时复制覆盖。
- 提交前验证 manifest 的序号唯一、文件实际可解码、尺寸和 hash 正确。当前 _persist_artifact_images 在 width/height 非零时不会实际用 Pillow 验证文件。

验收：正常修改第一张、保留第二张；原图缺失；原图 hash 变化；新渲染只返回一张；源文件同名。失败时不能产生可审批错误图片。

修正现有 test_revision_preserves_only_named_image_in_final_manifest：其 fake renderer 第二张每次均为同一蓝色，即使没执行保留逻辑也可能 hash 相同。应让每次渲染的第二张内容不同，验证保留逻辑确实生效，并穿透最终 draft_images/通知资产。

## N4（P2，恢复及费用验收前完成）：重启后的调用计数与预算

源码检查：runner 将 _runtime_model_calls 持久化，但新 LiveRuntime.model_calls 从 0 开始。恢复后 current 只有超过旧 seen 时才累计；例如旧 seen=3，新 runtime 第 1–3 次调用会漏计。PhysicalFetchQuota 构造时也重新分配全量，需核对是否从持久消耗扣减。

实现：区分累计持久调用数和本进程增量基线；统一计数决策、writer、critic 及实际请求重试。每次调用前预留剩余额度，不能只在执行工具后累计。恢复不应重置整个 run 的物理抓取预算。

deferred 恢复目前只重置 elapsed_s；若因 max_model_calls 耗尽而暂停，仍会立即再次 deferred。明确不同原因的恢复条件和入口：临时超时可恢复，预算耗尽需显式增加限额或进入新预算窗口。不要通过无条件清零累计消耗假装恢复成功。

验收：跨两次新 runtime 继续调用，累计数等于观测到的真实调用数，恰好达到限额时不再追加调用；单次工具和 critic 不能越过剩余额度。抓取数同理。语义审稿缓存可继续实现为按正文/证据/图片/审稿策略 hash 复用，内容变化必须失效。

## 固定交付清单

1. 完成 N1–N4；补真实 inbound → worker → 第一轮模型 observation → 修订 → 审批穿透测试，不再只手工传父稿给 runtime。
2. 在当前主项目运行相关回归和全量回归，保留命令、计数、日志。处理测试 event loop 的释放，避免资源警告长期残留。
3. 更新交付报告：已部署旧 V2 与本地未部署的 migration 10 版本分开说明。删掉“A5 材料持久化未完成”等已过时描述，保留确实未完成项；进度中“下一步完成全量”也应与最新全量结果一致。
4. 交付可执行的部署/回滚步骤，列出目标 schema、备份路径规则、启动后健康检查和预期日志；本批先不执行生产迁移或发帖。
5. 不要求分布式 exactly-once，也不引入新调度框架：保证本机 SQLite 下上述具体失败窗口可恢复即可。不要把抽象的“完全原子化”作为无限扩展任务。

## 下一阶段：条件通过后进入手机验收

N1–N4 与相关测试完成后，先复查，再部署 migration 10 并启用经过预算验证的语义审稿。由用户手机依次验收：普通选题成稿；针对该稿修改第一张且保留第二张；运行中重启恢复；旧审批不能再发布。最后由用户批准一篇最终稿，验证一次真实发布和回执。

上述通过后才做下一功能批：定时自主选题、ask_owner 的问答恢复、课程对照评估。无需等待长期偏好学习或视觉模型才能开始手机验收。
