"""Identifier helpers.

All identifiers are opaque strings; nothing security relevant is derived from
their ordering.  Approval codes are human readable but are always looked up
together with the full version fingerprint before a publish can happen.
"""

# 中文说明：
# 这个文件只做一件事：生成各种“编号”。可以把它想成打印票据的机器：
#   - new_id()        给内部记录生成几乎不会重复的长编号
#   - base36()        把一个整数换成 0-9a-z 的短字符串
#   - approval_code() 生成给人看的审批码，例如 XH-7K2M9Q
#   - item_key()      根据几段文本算出一个固定指纹，用来去重
#
# 为什么审批码要排除 0/O、1/I/L？
#   用户要在手机上手打这个码。长得像的字符最容易打错，所以字母表里直接
#   不放这些字符，减少“明明批准了却提示找不到审批码”的挫败感。
#
# 注意：审批码本身不是密码。真正的安全来自“只有绑定的 owner 能发命令”
# 以及“审批绑定正文哈希、图片哈希、账号、时间”，光猜到码没有用。

from __future__ import annotations

import secrets
import time

# 手机输入友好的字符集：去掉了容易混淆的 0/O、1/I/L。
# 一共 31 个字符，6 位大约有 31^6 ≈ 8.8 亿种组合。
# No ambiguous characters (0/O, 1/I/L) for codes typed on a phone.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def new_id(prefix: str = "") -> str:
    """Return a roughly sortable, collision resistant identifier."""
    # 思路：时间戳在前（所以大致按创建时间排序）+ 随机数在后（防碰撞）。
    # time.time_ns() 是纳秒，base36 后比十进制短很多。
    stamp = base36(time.time_ns())
    # secrets.token_hex 使用操作系统安全随机源，比 random 更适合生成编号。
    rand = secrets.token_hex(5)
    return f"{prefix}_{stamp}_{rand}" if prefix else f"{stamp}_{rand}"


def base36(value: int) -> str:
    # 36 进制转换：不断除以 36 取余数，余数查表变成字符。
    # 例如 36 -> "10"，1296 -> "100"。没有外部依赖，逻辑一目了然。
    if value < 0:
        raise ValueError("value must be non-negative")
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = "0123456789abcdefghijklmnopqrstuvwxyz"[rem] + out
    return out or "0"


def approval_code() -> str:
    """A short code such as ``XH-7K2M9Q`` (not a secret by itself)."""
    # 用 secrets.choice 而不是 random.choice：random 的序列可被预测，
    # 审批码虽然不算密码，但也不该让人轻易枚举。
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
    return f"XH-{body}"
