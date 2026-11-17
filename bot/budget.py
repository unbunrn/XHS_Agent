"""Conservative daily API budget accounting.

DeepSeek list prices are denominated in USD, so the ledger is kept in USD and
never converted with an implicit exchange rate.  Responses without usage data
are marked unknown and *reserved* at a pessimistic configurable amount instead
of being counted as zero.
"""

# =============================================================================
# 中文导读：给“调用大模型”装一个每日钱包
# =============================================================================
# 为什么需要它？
#   大模型按 token 收费。如果程序陷入死循环、反复重试，或者某天热点新闻太多，
#   账单可能悄悄涨到很高。BudgetGuard 就是钱包 + 记账本 + 保险丝：
#     - 每次调用前先估算要花多少钱，超过当日预算就拒绝；
#     - 每次调用后把实际花费写进 llm_usage 表；
#     - 当天花超了就“暂停生成”，第二天自动恢复。
#
# 为什么用美元？
#   DeepSeek 的价目表以美元计价。如果在代码里偷偷按某个汇率换成人民币，
#   汇率一变账就对不上。所以账本统一记 USD，展示时标明 currency。
#
# 最保守的一条规则：用量未知
#   如果模型服务没有返回 token 用量，我们不能当成“免费”，而是按一个
#   偏高的固定金额“预留”（reserve_unknown_usd）。就像打车时不知道最终
#   车费，先按最贵路线冻结一笔钱，避免钱包被悄悄刷爆。
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass

from .adapters.types import BudgetExceeded
from .config import Config
from .db import Database, get_setting, set_setting
from .timeutil import Clock, local_date, local_day_bounds_utc, to_iso


@dataclass
class BudgetSnapshot:
    # 某一时刻的“钱包快照”，用于展示和判断。
    # day 是本地日期（按配置时区），用户看到的“今天”和自己日历一致。
    day: str
    calls: int
    spent_usd: float
    reserved_usd: float
    daily_usd: float
    max_calls: int
    paused: bool
    pause_reason: str | None

    @property
    def used_usd(self) -> float:
        # “已用” = 实际花费 + 未知用量的预留。
        # 预留不是真的花掉，但在做预算判断时必须当成已占用，
        # 否则可能同时发起多笔调用，把预算击穿。
        return round(self.spent_usd + self.reserved_usd, 6)

    @property
    def remaining_usd(self) -> float:
        # 剩余额度不允许为负数：展示 -0.01 美元只会让人困惑。
        return max(0.0, round(self.daily_usd - self.used_usd, 6))

    def as_dict(self) -> dict:
        # 给控制台 / CLI 输出的字典版本；金额统一保留 6 位小数，
        # 因为单次调用可能只花 0.0003 美元。
        return {
            "day": self.day,
            "calls": self.calls,
            "spent_usd": round(self.spent_usd, 6),
            "reserved_usd": round(self.reserved_usd, 6),
            "used_usd": self.used_usd,
            "daily_usd": self.daily_usd,
            "max_calls": self.max_calls,
            "remaining_usd": self.remaining_usd,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "currency": "USD",
        }


class BudgetGuard:
    # 依赖注入：数据库、配置、时钟都从外面传入。
    # 好处是测试可以用“冻结的时钟”和临时数据库，不必改系统时间。
    def __init__(self, db: Database, config: Config, clock: Clock):
        self.db = db
        self.config = config
        self.clock = clock

    def _snapshot_conn(self, conn) -> BudgetSnapshot:
        # 统计“今天”的用量。注意数据库里的 at 存的是 UTC，
        # 而“今天”按用户配置的时区算，所以要先把本地日界换算成 UTC 区间。
        # 例如北京时间 11 月 5 日的 0 点，对应 UTC 11 月 4 日 16 点。
        # 这个版本接收调用方连接，预留/结算可以在同一个写事务里核对余额。
        now = self.clock.now()
        day = local_date(now, self.config.timezone_name)
        start, end = local_day_bounds_utc(now, self.config.timezone_name)
        row = conn.execute(
            "SELECT COUNT(*) AS calls, "
            "COALESCE(SUM(cost_usd), 0) AS spent, "
            "COALESCE(SUM(reserved_usd), 0) AS reserved "
            "FROM llm_usage WHERE at >= ? AND at < ?",
            (to_iso(start), to_iso(end)),
        ).fetchone()
        paused_on = get_setting(conn, "generation_paused_on")
        reason = get_setting(conn, "generation_pause_reason")
        # 暂停只对“暂停那一天”有效：第二天 paused_on 不等于今天的 day，
        # 自动恢复生成。不需要定时任务专门去解除暂停。
        paused = paused_on == day
        return BudgetSnapshot(
            day=day,
            calls=int(row["calls"]) if row else 0,
            spent_usd=float(row["spent"]) if row else 0.0,
            reserved_usd=float(row["reserved"]) if row else 0.0,
            daily_usd=float(self.config.get("budget.daily_usd", 0.10)),
            max_calls=int(self.config.get("budget.max_calls_per_day", 40)),
            paused=paused,
            pause_reason=reason if paused else None,
        )

    def snapshot(self) -> BudgetSnapshot:
        with self.db.read() as conn:
            return self._snapshot_conn(conn)

    def estimate_usd(self, prompt_tokens: int, max_output_tokens: int | None = None) -> float:
        # 调用前的保守估算：输入 token 数基本已知，输出按“最大可能值”算，
        # 这样不会因为低估而超支。真实花费以 settle() 的 usage 结算为准；
        # 该估算不是硬性货币上限。
        input_price = float(self.config.get("budget.input_usd_per_million", 0.30))
        output_price = float(self.config.get("budget.output_usd_per_million", 1.20))
        output_tokens = int(max_output_tokens or self.config.get("budget.max_output_tokens", 1800))
        return (prompt_tokens * input_price + output_tokens * output_price) / 1_000_000.0

    def ensure_available(self, estimated_usd: float) -> BudgetSnapshot:
        # 三道闸门，任意一道不过就拒绝调用：
        #   1. 今天是否已被暂停（之前已经超限）；
        #   2. 调用次数是否达到上限（防止小额调用刷爆次数）；
        #   3. 已用 + 本次预估是否超过每日金额。
        # 被拒绝时抛出 BudgetExceeded，这是“保护生效”，不是 bug。
        snapshot = self.snapshot()
        if snapshot.paused:
            raise BudgetExceeded(f"generation paused for {snapshot.day}: {snapshot.pause_reason}")
        if snapshot.calls >= snapshot.max_calls:
            self._pause(snapshot.day, f"daily call limit reached ({snapshot.max_calls})")
            raise BudgetExceeded(f"daily call limit reached ({snapshot.max_calls})")
        if snapshot.used_usd + max(estimated_usd, 0.0) > snapshot.daily_usd:
            self._pause(
                snapshot.day,
                f"daily budget limit reached (used {snapshot.used_usd:.4f} + est "
                f"{estimated_usd:.4f} > {snapshot.daily_usd:.4f} USD)",
            )
            raise BudgetExceeded("daily API budget would be exceeded")
        return snapshot

    def reserve(
        self,
        estimated_usd: float,
        *,
        provider: str,
        model: str,
        purpose: str,
        ref: str | None = None,
    ) -> int:
        """Atomically reserve budget before making a network request.

        The daily balance check and the reservation insert happen in one
        ``BEGIN IMMEDIATE`` transaction, so two concurrent model calls cannot
        both spend the last remaining budget.  ``estimated_usd`` is a
        conservative token-price estimate, not a hard currency ceiling; the
        provider invoice remains authoritative.
        """
        estimate = max(float(estimated_usd), 0.0)
        now = to_iso(self.clock.now())
        denied: str | None = None
        with self.db.tx() as conn:
            snapshot = self._snapshot_conn(conn)
            if snapshot.paused:
                # No write is needed; raising rolls back nothing.
                raise BudgetExceeded(
                    f"generation paused for {snapshot.day}: {snapshot.pause_reason}"
                )
            if snapshot.calls >= snapshot.max_calls:
                denied = f"daily call limit reached ({snapshot.max_calls})"
                self._pause_conn(conn, snapshot.day, denied)
            elif snapshot.used_usd + estimate > snapshot.daily_usd:
                denied = "daily API budget would be exceeded"
                self._pause_conn(
                    conn,
                    snapshot.day,
                    f"daily budget limit reached (used {snapshot.used_usd:.4f} + est "
                    f"{estimate:.4f} > {snapshot.daily_usd:.4f} USD)",
                )
            else:
                cur = conn.execute(
                    "INSERT INTO llm_usage(at, provider, model, purpose, ref, prompt_tokens, "
                    "completion_tokens, total_tokens, usage_known, cost_usd, reserved_usd, "
                    "latency_ms, ok, error, settled) "
                    "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 0, 0, ?, NULL, 1, NULL, 0)",
                    (now, provider, model, purpose, ref, estimate),
                )
                reservation_id = int(cur.lastrowid)
                if snapshot.used_usd + estimate >= snapshot.daily_usd:
                    self._pause_conn(conn, snapshot.day, "daily API budget fully reserved")
                return reservation_id
        # The pause flag above must commit before we tell the caller "denied";
        # raising inside the transaction would roll the pause back.
        raise BudgetExceeded(denied or "daily API budget would be exceeded")

    def settle(
        self,
        reservation_id: int,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        usage_known: bool,
        latency_ms: int = 0,
        ok: bool = True,
        error: str | None = None,
    ) -> float:
        """Settle a reservation with actual usage (or retain the estimate)."""
        input_price = float(self.config.get("budget.input_usd_per_million", 0.30))
        output_price = float(self.config.get("budget.output_usd_per_million", 1.20))
        known = bool(
            usage_known and prompt_tokens is not None and completion_tokens is not None
        )
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT reserved_usd FROM llm_usage WHERE id=? AND settled=0",
                (int(reservation_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"budget reservation {reservation_id} is missing or already settled")
            held = float(row["reserved_usd"] or 0.0)
            if known:
                cost = (
                    int(prompt_tokens or 0) * input_price
                    + int(completion_tokens or 0) * output_price
                ) / 1_000_000.0
                reserved = 0.0
            else:
                # No usable usage data: keep the reservation instead of
                # pretending the call was free.
                cost = 0.0
                reserved = held if held > 0 else float(
                    self.config.get("budget.reserve_unknown_usd", 0.005)
                )
            conn.execute(
                "UPDATE llm_usage SET prompt_tokens=?, completion_tokens=?, total_tokens=?, "
                "usage_known=?, cost_usd=?, reserved_usd=?, latency_ms=?, ok=?, error=?, settled=1 "
                "WHERE id=? AND settled=0",
                (
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    1 if known else 0,
                    cost,
                    reserved,
                    latency_ms,
                    1 if ok else 0,
                    (error or "")[:500] or None,
                    int(reservation_id),
                ),
            )
            if ok:
                set_setting(conn, "last_llm_success_at", now)
            snapshot = self._snapshot_conn(conn)
            if snapshot.used_usd >= snapshot.daily_usd:
                self._pause_conn(conn, snapshot.day, "daily API budget exhausted")
        return cost + reserved

    def settle_fixed(
        self,
        reservation_id: int,
        *,
        cost_usd: float,
        ok: bool = True,
        error: str | None = None,
        latency_ms: int = 0,
    ) -> float:
        """Settle a per-call fixed-cost reservation (Search/Fetch).

        ``cost_usd=0`` is used only for a known empty response, which the
        provider documents as free.  Ambiguous failures must use
        ``retain_unknown`` instead so the conservative hold stays in place.
        """
        amount = max(0.0, float(cost_usd))
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT reserved_usd FROM llm_usage WHERE id=? AND settled=0",
                (int(reservation_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"budget reservation {reservation_id} is missing or already settled")
            conn.execute(
                "UPDATE llm_usage SET usage_known=1, cost_usd=?, reserved_usd=0, latency_ms=?, "
                "ok=?, error=?, settled=1 WHERE id=? AND settled=0",
                (
                    amount,
                    latency_ms,
                    1 if ok else 0,
                    (error or "")[:500] or None,
                    int(reservation_id),
                ),
            )
            if ok:
                set_setting(conn, "last_llm_success_at", now)
            snapshot = self._snapshot_conn(conn)
            if snapshot.used_usd >= snapshot.daily_usd:
                self._pause_conn(conn, snapshot.day, "daily API budget exhausted")
        return amount

    def retain_unknown(
        self,
        reservation_id: int,
        *,
        error: str,
        latency_ms: int = 0,
    ) -> float:
        """Finish a failed/ambiguous call while retaining its reservation."""
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT reserved_usd FROM llm_usage WHERE id=? AND settled=0",
                (int(reservation_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"budget reservation {reservation_id} is missing or already settled")
            held = float(row["reserved_usd"] or 0.0)
            reserved = held if held > 0 else float(
                self.config.get("budget.reserve_unknown_usd", 0.005)
            )
            conn.execute(
                "UPDATE llm_usage SET usage_known=0, cost_usd=0, reserved_usd=?, "
                "latency_ms=?, ok=0, error=?, settled=1 WHERE id=? AND settled=0",
                (reserved, latency_ms, (error or "")[:500] or None, int(reservation_id)),
            )
            snapshot = self._snapshot_conn(conn)
            if snapshot.used_usd >= snapshot.daily_usd:
                self._pause_conn(conn, snapshot.day, "daily API budget exhausted")
        return reserved

    def _pause_conn(self, conn, day: str, reason: str) -> None:
        set_setting(conn, "generation_paused_on", day)
        set_setting(conn, "generation_pause_reason", reason[:300])

    def record(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        usage_known: bool,
        latency_ms: int = 0,
        ref: str | None = None,
        ok: bool = True,
        error: str | None = None,
        reserved_usd: float | None = None,
    ) -> float:
        # 记账。分两种情况：
        #   - 用量已知：按输入/输出 token 分别乘单价，算出精确费用；
        #   - 用量未知：不记 0，而是记一笔“预留金额”，宁可多算不可少算。
        # 返回值是本次“花费 + 预留”，调用方可用于日志。
        input_price = float(self.config.get("budget.input_usd_per_million", 0.30))
        output_price = float(self.config.get("budget.output_usd_per_million", 1.20))
        cost = 0.0
        reserved = 0.0
        if usage_known and prompt_tokens is not None and completion_tokens is not None:
            cost = (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000.0
        else:
            reserved = (
                float(reserved_usd)
                if reserved_usd is not None
                else float(self.config.get("budget.reserve_unknown_usd", 0.005))
            )
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO llm_usage(at, provider, model, purpose, ref, prompt_tokens, completion_tokens, "
                "total_tokens, usage_known, cost_usd, reserved_usd, latency_ms, ok, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    to_iso(self.clock.now()),
                    provider,
                    model,
                    purpose,
                    ref,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    1 if usage_known else 0,
                    cost,
                    reserved,
                    latency_ms,
                    1 if ok else 0,
                    (error or "")[:500] or None,
                ),
            )
            if ok:
                set_setting(conn, "last_llm_success_at", to_iso(self.clock.now()))
        snapshot = self.snapshot()
        # 记完账再检查一次：如果这一笔刚好把预算用光，立即暂停，
        # 避免下一个任务继续发起调用。
        if snapshot.used_usd >= snapshot.daily_usd:
            self._pause(snapshot.day, "daily API budget exhausted")
        return cost + reserved

    def _pause(self, day: str, reason: str) -> None:
        # 只记录“哪一天暂停”和“为什么”，不修改配置。
        # 原因是配置是用户的东西，运行状态不该偷偷改配置文件。
        with self.db.tx() as conn:
            set_setting(conn, "generation_paused_on", day)
            set_setting(conn, "generation_pause_reason", reason[:300])

    def resume_generation(self, reason: str = "manual resume") -> None:
        # 人工解除暂停（控制台按钮或 CLI 命令）。
        # 把日期清空，今天就会重新允许生成；reason 仅用于审计展示。
        with self.db.tx() as conn:
            set_setting(conn, "generation_paused_on", "")
            set_setting(conn, "generation_pause_reason", reason)
