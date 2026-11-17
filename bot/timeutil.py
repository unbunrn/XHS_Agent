"""Time handling.

Everything is persisted in UTC ISO-8601 with a ``Z`` suffix.  User facing
schedules are evaluated in the configured timezone (Asia/Shanghai by default).
"""

# 中文说明：
# 这个文件是“时间管家”，解决三个很容易出错的问题：
#   1. 数据库里统一存 UTC（世界标准时间），避免夏令时/时区切换造成混乱；
#   2. 用户看到的时间（例如“晚上 8 点发布”）按本地时区（默认上海）计算；
#   3. 测试需要“冻结时间”，让结果可重复，所以提供 FrozenClock。
#
# 关键概念：
#   - UTC：全世界统一的时间基准，不带时区偏移；
#   - ISO-8601：形如 2026-11-05T09:00:00.000000Z 的字符串；
#   - 存储用 UTC，展示用本地时区，两者绝不混用。

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Protocol

UTC = timezone.utc
_ISO_Z = re.compile(r"Z$")
# 时区对象创建有一定开销，而且 ZoneInfo 查表也可能失败；
# 用缓存避免每次格式化时间都重新构造。
_TZ_CACHE: dict[str, tzinfo] = {}


class Clock(Protocol):
    # 协议（Protocol）只是一种“形状约定”：任何对象只要有 now() 方法，
    # 就能当 Clock 用。测试里的 FrozenClock 和真实的 SystemClock 都符合。
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        # 真实运行时永远使用带 UTC 时区的当前时间。
        return datetime.now(UTC)


class FrozenClock:
    """Test clock.  Kept in the package so tests and demos share one clock."""

    def __init__(self, value: datetime | None = None) -> None:
        # 不传时间时默认固定在 2026-11-05 09:00 UTC，
        # 这样每次跑测试的起点都一样，结果可重复。
        self._value = (value or datetime(2026, 11, 5, 9, 0, tzinfo=UTC)).astimezone(UTC)

    def now(self) -> datetime:
        return self._value

    def advance(self, **kwargs: float) -> datetime:
        # 例如 clock.advance(hours=24) 表示“快进 24 小时”。
        # timedelta 支持 days/seconds/microseconds 等关键字。
        self._value = self._value + timedelta(**kwargs)
        return self._value

    def set(self, value: datetime) -> None:
        self._value = value.astimezone(UTC)


def get_timezone(name: str) -> tzinfo:
    """Resolve an IANA zone, falling back to a fixed +08:00 for Asia/Shanghai.

    A stock Windows Python often has no IANA database installed.  The fallback
    is exact for Asia/Shanghai (no DST) and is surfaced by ``doctor``.
    """
    if name in _TZ_CACHE:
        return _TZ_CACHE[name]
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(name)
    except Exception:
        # Windows 上常见问题：系统没装 IANA 时区数据库，ZoneInfo 会抛异常。
        # 上海没有夏令时，所以固定 +08:00 与真实情况完全一致。
        # 其他时区不硬猜，直接把异常抛出去，让调用方看到明确错误。
        if name in {"Asia/Shanghai", "Asia/Chongqing", "PRC"}:
            zone = timezone(timedelta(hours=8), "Asia/Shanghai")
        else:
            raise
    _TZ_CACHE[name] = zone
    return zone


def to_iso(value: datetime) -> str:
    # 统一出口：不论传进来的是本地时间还是 UTC，都先转 UTC，再格式化成
    # 带微秒、以 Z 结尾的字符串。数据库里的所有时间都长这样。
    if value.tzinfo is None:
        # “没有时区信息”的时间是危险的；这里约定按 UTC 解释，
        # 而不是悄悄使用本机时区。
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def utcnow_iso() -> str:
    return to_iso(datetime.now(UTC))


def parse_iso(value: str | None) -> datetime | None:
    # 解析失败返回 None 而不是抛异常，方便调用方用 if 判断。
    # 把结尾的 Z 换成 +00:00，因为较老的 Python fromisoformat 不认 Z。
    if not value:
        return None
    text = _ISO_Z.sub("+00:00", value.strip())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_feed_datetime(value: str | None) -> tuple[datetime | None, str]:
    """Parse common RSS/Atom date formats.

    Returns ``(utc_datetime_or_None, precision)`` where precision is
    ``time``/``date``/``unknown``.  A date-only value is not treated as an
    exact publication instant.
    """
    # 为什么需要“精度（precision）”？
    #   有些来源只写“2026-11-05”，没写几点几分。如果系统把它当成
    #   当天 00:00，再算“距今多少小时”，就会把旧闻误判成新热点。
    #   所以这里明确标成 date/unknown，打分和文案都能据此保守处理。
    if not value:
        return None, "unknown"
    raw = " ".join(str(value).split())
    if not raw:
        return None, "unknown"
    candidates = [raw]
    # RFC 822 变体，例如 "Wed, 05 Nov 2026 09:00:00 GMT"。
    # 这些老格式在 RSS 里非常常见，strptime 需要逐个尝试。
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M %z",
        "%d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        candidates.append(fmt)
    for item in candidates:
        if "%" in item:
            # 这一项是 strptime 格式串，不是原始文本。
            try:
                parsed = datetime.strptime(raw, item)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC), "time"
        else:
            # 这一项是待解析的原始文本，交给 fromisoformat。
            try:
                parsed = datetime.fromisoformat(_ISO_Z.sub("+00:00", item))
            except ValueError:
                continue
            # 长度 <=10 说明只有年月日（YYYY-MM-DD），没有具体时刻。
            precision = "date" if len(item) <= 10 else "time"
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC), precision
    # 最后再兜底尝试“纯日期”。
    # Date only, e.g. 2026-11-05
    try:
        parsed_date = datetime.strptime(raw[:10], "%Y-%m-%d")
        return parsed_date.replace(tzinfo=UTC), "date"
    except ValueError:
        return None, "unknown"


def local_iso(value: datetime, tz_name: str) -> str:
    # 给人和给 UI 看的格式：转成本地时区，精确到分钟就够了。
    return value.astimezone(get_timezone(tz_name)).isoformat(timespec="minutes")


def next_publish_slot(now: datetime, tz_name: str, hour: int, minute: int = 0) -> datetime:
    """Next occurrence of a local wall-clock slot, returned in UTC."""
    # 例如配置为本地 20:00 发布：
    #   - 现在是本地 19:00 -> 返回今天 20:00（UTC 表示）
    #   - 现在是本地 21:00 -> 今天已过点，返回明天 20:00
    # 先换成本地时间再替换时分秒，避免自己手算时区偏移。
    tz = get_timezone(tz_name)
    local = now.astimezone(tz)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(UTC)


def local_day_bounds_utc(now: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """UTC bounds of the local calendar day containing ``now``."""
    # “今天最多发 1 篇”里的“今天”必须是用户所在地的今天，
    # 而不是 UTC 的今天。这里算出本地当天 00:00 到次日 00:00 的 UTC 区间，
    # 数据库查询就能用半开区间 [start, end) 统计。
    tz = get_timezone(tz_name)
    local = now.astimezone(tz)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def local_date(now: datetime, tz_name: str) -> str:
    # 返回 YYYY-MM-DD，用作“每日预算”等按天去重的键。
    return now.astimezone(get_timezone(tz_name)).date().isoformat()


def human_delta(seconds: float) -> str:
    # 把秒数变成人能一眼看懂的大致时长：90 -> "1m"，7200 -> "2h"。
    # 只用于展示，不参与业务判断。
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
