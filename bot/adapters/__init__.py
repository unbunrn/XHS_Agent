"""External and fake adapters for sources, LLM, notification and publishing."""

# 中文说明：
# 这个包里放的是“适配器”（adapter）。适配器就像电器上的转接头：
#   - 内部业务代码只认统一的接口（比如 fetch / complete_json / publish）；
#   - 外面无论是 RSS 还是本地 fixture、DeepSeek 还是假模型、飞书还是本地文件、
#     小红书还是 mock 导出，都由适配器翻译成同一种格式。
#
# 一条重要规矩：适配器不直接改数据库。它们最多返回结果或抛异常，
# “要不要写库、状态怎么变”由 bot/services/ 里的业务代码决定。
# 这样测试时可以换成假适配器，也不会绕过审批等安全规则。
