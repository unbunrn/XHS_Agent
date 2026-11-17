# S1–S3 独立验收：主流程仍需返修

> 返修状态（2026-11-11）：R1–R4 已处理并补端到端回归，结果见 [S1-S3-research-delivery.md](S1-S3-research-delivery.md) 的“独立验收 R1–R4 返修”节。以下保留本轮独立验收时的原始结论，作为历史记录；仍不部署、不调用真实账号。

日期：2026-11-11（Asia/Shanghai）。结论：**暂不部署，先修以下四项。** 本轮仅离线代码检查、假响应验证与文档更新；没有真实 API、生产数据库、飞书发送、发帖或服务重启。

项目：本机 `D:\Dev\Projects\bot`。DSH 自报 243 项全套测试通过，不代表端到端要求已满足。下面将已复现事实和静态检查结论分别列明。

## R1 / P1：新来源正文没有传入身份核验，S2 主流程未接通

位置：`bot/services/topic_research.py` try_candidate 的 domain_unverified 分支及搜索循环；`bot/services/research_orchestrator.py` run_plan；`bot/services/topics.py:1055`。

搜索候选未验证时返回 False，不进入 outcome.entries。未验证直链虽暂存为候选，也未进入交给身份核验的材料。topics 只有 matched 非空才调用 verify_source_material。结果是必须已经在配置中可信，才有机会进入用来验证新来源的 S2。

独立复现：同一任务三轮都取得相关、日期有效的新域名正文，最终 blocked；model_purposes 仅有 research_plan，没有 source_identity；三条诊断均为 domain_unverified。日志 `var/tests/parent-s123-probe.log`。

修复：独立维护候选材料和可写稿证据，候选材料应进入一次批量身份/相关性判断，只有核验通过后进入 evidence。不要直接把 unknown 放入可信 entries。规划执行不能一遇到任意现有可信文章就停止，导致待核验的新站及其关系证据永远没有机会收集；停止条件应按足够的核验材料设计，并遵守六次总 Fetch。

必须增加从 process_topic_request 开始的测试：新产品站 + 已取得的可信官网关系材料，真实执行批量核验后生成审批稿；假站不得生成。现有直接传两个 entries 给 verify_source_material 的单元测试不能替代接线测试。

## R2 / P1：域名文字出现被当成官方链接关系

位置：`bot/services/source_identity.py:90` 的 `_link_relation`。

目前只判断 `candidate_host in content`，没有提取链接或确认机构关系。实际反例：可信页面正文写“Beware vendor.example: it is an impersonation site, not our official site.”，模型输出 official/trusted_link 后，新域名仍得到 verified=true、relation=trusted_link:E1。

这是核验函数层面的可复现误放行；当前 R1 使部分新域名进不了该函数，不能依靠这个接线缺陷当防护。修 R1 必须同时修 R2。

修复：从实际抓取结果保留可审计的链接目标、上下文、来源材料 ID、机构/路径作用域。仅文字提及、警告/否定、相似主机名、第三方托管平台链接不能证明官方归属；真实链接也只是关系证据，需验证与所声明机构/产品的关系。程序校验 URL/host/路径与实际材料吻合，不能采用模型编造的关系。当前 E 编号按 trusted_entries 重新枚举，也须与完整材料的稳定 ID 对齐。

测试：真实官网明确链接产品站通过；假冒警告、纯文本域名、域名子串碰撞、平台不同组织、伪造关系 ID 均不能通过；身份缓存命中必须重验关系，不能保留先前误判。

## R3 / P1：模型异常或预算耗尽降级成核验通过

位置：`source_identity.py:291`、`_fallback`；`research_plan.py` 的异常捕获；`research_orchestrator.py` 的预算分类；topics 身份调用异常分流。

独立复现：身份模型抛 BudgetExceeded，基础可信域名直接得到 verified=true、supported_claims=[]。这把“来源地址已配置”当成“内容已完成核验”，绕过新加的主张判断。不能用此 fallback 提高成功率。

另一个反例：run_plan 捕获 BudgetExceeded 后写 `budget_exhausted: ...`，但最终只检测字符串 `Budget`，结果为 failed，而不是 deferred。

修复：BudgetExceeded 单独透传/映射 deferred，规划、身份核验、研究编排和入口一致处理；模型故障不得自动认定内容已核实。已配置来源身份可以保留，但内容核验状态应未完成；若需降级，必须有真实的确定性内容支持检查，并在报告中明确边界。使用类型或结构化错误码，不根据英文大小写猜错误类型。不得因规划失败悄悄把预算错误转换成一般规划回退继续收费。

测试：规划、搜索、抓取、身份、写稿各阶段预算耗尽均正确终止；identity 故障不能新增 verified evidence；attempt/request 终态一致；已发送计数准确，无新增不必要调用。

## R4 / P2：analysis 仍强制使用近期搜索过滤

静态定位：`topics.py:1014` 附近固定 lookback；`bot/adapters/sources.py` `_search` 每次移除原 after 再强制添加最近 lookback_days 的 after（最大 30 天）。

analysis 虽放宽正文时效到五年，查询却仍按最近约七天发现文章；模型无法通过计划真正移除该过滤。没有直链的旧文解读仍可能搜不到原文。报告“允许旧材料”不等于“能搜索到旧材料”。本项是代码路径确认，未调用真实搜索验证召回。

修复：引入明确可选发现日期策略，让 analysis 查询可不加近期 after；news 必要时也可有限放宽发现范围，但证据采纳继续按 news 时效。日期模式不能由网页/模型私自改变。缓存 key 纳入日期发现策略。

测试使用记录实际 `_post_once` 搜索 payload 的假 provider：analysis 旧研究无直链时能检索并走到合法审批；news 保持最终时效校验；明确无过滤时 adapter 不偷偷重加。不得全局把 max_age 改大。

## 交付与测试要求

复现脚本：`var/tests/parent_s123_probe.py`；输出：`var/tests/parent-s123-probe.log`。均为假域名/假正文/测试库，无实际秘密。

本轮不要求继续增加功能。DSH 先把以上四项转为回归测试，修复后执行 S1–S3 相关测试，再做必要的审批/发布/配额回归。新来源必须有端到端测试，不能只测孤立函数。保持离线边界，不读 secrets/Cookie/var/live，不部署、不发消息、不发帖。完成后统一更新 S1-S3-research-delivery.md 和 README，列真实结果和剩余限制。

交付报告提及 topics.py 曾从 pyc 反编译恢复。本轮未确认恢复前后的等价性，不据此断言代码丢失；返修时保留恢复记录，重点覆盖既有审批、任务去重和人工配额路径，不再通过反编译替代源码备份。

## 独立测试结果

本轮实际运行：`python -m unittest tests.test_research_pipeline_s1 tests.test_research_pipeline_s1_review tests.test_research_s2 tests.test_s3_modes tests.test_migration_s8 -q -b`，**49 tests，106.462s，OK**。日志 `var/tests/parent-s1-s3-review.log`。现有用例通过，但上述独立反例表明覆盖不足。未重复执行全套 243 项，未部署或验证真实 provider。
