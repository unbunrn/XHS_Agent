# P0 独立验收与返修清单

检查日期：2026-11-10（北京时间）。结论：暂不通过部署验收。只检查代码及离线临时数据，没有修改业务源码、生产配置、生产库，没有重启服务、发送消息或发布。本报告不评价未实现P1等阶段。

## 已验证

独立运行 `python -m unittest tests.test_publication_results tests.test_publish_quota_recovery tests.test_split_publish_quota -q`：14 tests，OK，26.794s。日志 `var/tests/parent-p0-review.log`。DSH报告186项全测通过属于其交付记录，本次未重复全测。

新增模块职责清楚，迁移以追加方式新增，结果与通知复用现有持久化机制。未发现主动重复发帖的新增路径。真实小红书只读provider仍不可用，CLI默认UnavailableEvidenceProvider，因此只能称“结果通知与对账框架”，不能称真实自动对账已完成。

## P1-1：缺少强证据仍认领成功

位置：bot/services/reconcile_readonly.py:129、156（_match_note）。
最小复现：approval.account_id='owner'，draft.title='same title'，draft.body='approved substantial body'；attempt.started_at='2026-11-05T09:00:00Z'；候选external_post_id='unrelated'、title相同、published_at='2026-11-05T09:30:00Z'、body=''、account_id=None；window=120，similarity=.65。
实际返回 `(True, 'unique title/time/body match', 0.0)`。正文相似度0、账号未知，仅标题和时间也能确认发布。
修复：明确provider的本人账号证明契约，缺少账号证明/关键内容证据保持unknown；相近时间与同标题不能单独构成强匹配。已有强回执关联可以另走可靠路径，不要一刀切阻断可证实匹配。
验收：空正文+未知账号+同标题近时间必须unknown；错误账号、缺失时间、同标题不同正文、多候选分别测试；真正强且唯一的本人笔记可以认领。不得把测试改成接受弱匹配。

## P1-2：异步旧结果覆盖已确认发布

位置：bot/services/reconcile_readonly.py:236、382；bot/services/publication_results.py:139。同时检查publish.py的reconcile_unknown与_finish状态转换。
离线复现：先创建unknown尝试；注入EvidenceProvider，其lookup内部模拟另一操作者执行reconcile_unknown(confirmed_published, external_post_id='verified-fixture')，随后lookup返回空列表。外层read_only_reconcile将旧unknown写回publish_results。
实际：publish_attempts.state='published'，publish_results.state='unknown'。已确认成功与用户看到的结果不一致，还可能排队过时unknown通知。
修复：provider I/O完成后在写事务内重新读取尝试/审批当前状态，使用受控状态转换或CAS。终态不能被基于旧快照的unknown降级。不要对仍由活跃发布请求处理的publishing尝试抢先自动认领，除非有明确协调机制。对账检查记录与最终认领尽量同事务提交；限次/间隔也要避免并发穿透。
验收：注入provider期间发生人工成功认领、人工确认未发、另一个对账成功、原发布请求完成等交错；保留正确终态、回执ID和一致业务结果，不发送过时通知，不重复提交。普通unknown→published仍正常。

## P2-3：worker发布器未配置时漏结果与通知

位置：bot/worker.py:461-462。
离线复现：构造已审批测试稿，Worker.publisher=None，publisher_error='not configured'，调用_handle_publish（payload仅含approval_id）。
实际返回 `{'blocked': 'not configured'}`，publish_results新增行数0。绕过publish_approval里已有结果记录，真实worker仍会吞掉此类用户可见回执；仅直接测试publish_approval不能覆盖。
修复：将此路径接入统一发布业务结果服务/门禁记录，不直接返回无持久化字典；确保通知可重试且幂等，不创建publish_attempt。不要为了记录错误而强行调用不具备配置的发布器。
验收：通过Worker真实入口测试未配置发布器：1条blocked结果、1条通知、0次平台提交；再次执行不重复通知。

## 其他交付澄清

- 真实provider不可用是已披露限制，不要求编造接口。把“没有可靠文档化接口”表述为“本项目尚未实现并验证可靠provider”，避免绝对断言平台不存在能力。
- 生产数据库迁移未验证。部署前应在合成旧版本库验证迁移、重复初始化、旧记录保留；不得直接拿生产库跑测试。
- 当前record_result的stage字段在ON CONFLICT时未更新；应明确它表示初始阶段还是最新阶段，代码、CLI与报告一致。
- 交付报告称异常说明脱敏，但provider异常字符串目前直接进入detail；增加统一脱敏/截断，测试带查询token、凭据样式的异常不会进入通知或审计。不要使用真实密钥测试。

## 给DSH的返修指令

只修本文三个复现问题及直接相关防护，补充回归测试和交付记录；不做P1长期运行、不重构全项目、不读生产数据、不调用真实服务、不部署。不重新跑研究过程。先新增能复现问题的失败测试，再修正实现，最后跑相关测试；必要时跑一次全套，完成即停止。
保留用户当前自动1/人工5、预算5美元/200次、逐篇审批、发布不自动重试等规则。请更新原交付报告的限制与实际测试结果，不删除本报告。复核通过前不要宣称已部署或P0完全闭环。
