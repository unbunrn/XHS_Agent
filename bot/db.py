"""SQLite access layer with WAL, busy timeout and explicit transactions."""

# 中文说明：
# 这个文件是“数据库门房”，所有读写数据库的代码都从这里拿连接。
# 可以把它想成银行柜台：
#   - connect()/read()  只读窗口，办完就关；
#   - tx()              写业务窗口，要么全部成功，要么全部撤销（事务）；
#   - execute()         代办一个小事务；
#   - fetchall/fetchone 查询；
#   - backup_to()       把整个账本复制一份。
#
# 为什么用 SQLite？
#   单账号、单机、数据量不大，SQLite 不需要安装服务器，一个文件就是数据库。
#   配上 WAL 模式后，读写可以并行，进程崩溃也不容易丢数据。
#
# WAL 是什么？
#   传统模式写数据会锁住整个数据库；WAL（Write-Ahead Logging）先把改动
#   写进一个日志文件，读的人可以继续读旧数据，写完再合并。就像餐厅点单：
#   服务员先把新订单写在便签上，厨师按顺序做，不用把整个菜单锁起来。

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .schema import MIGRATIONS
from .timeutil import utcnow_iso


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 10_000):
        self.path = Path(path)
        # busy_timeout：数据库被别的连接锁住时，最多等这么久再报错。
        # 10 秒足够覆盖正常写入的短暂争用。
        self.busy_timeout_ms = busy_timeout_ms
        self._initialized = False

    # -- setup ----------------------------------------------------------
    def init(self) -> None:
        # 第一次使用时自动建目录、建表、跑迁移。
        # 初始化是幂等的：重复调用不会重复建表或重复执行迁移。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._raw_connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # schema_migrations 记录“已经执行到第几版”，升级时只跑新版本。
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            conn.execute("COMMIT") if conn.in_transaction else None
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current = int(row["v"] or 0)
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                # 单引号转义：SQLite 字符串用两个单引号表示一个单引号。
                stamp = utcnow_iso().replace("'", "''")
                # 每个迁移脚本自己包一个 BEGIN IMMEDIATE ... COMMIT：
                # 迁移要么整版生效，要么整版回滚，不会留下“半张表”。
                script = (
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({version}, '{stamp}');\n"
                    + "COMMIT;"
                )
                try:
                    conn.executescript(script)
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
        self._initialized = True

    def _raw_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,  # sqlite3 用秒，这里配置是毫秒
            isolation_level=None,  # None 表示手动控制事务，不让 sqlite3 自动 COMMIT
            check_same_thread=False,  # 控制台是多线程 HTTP 服务器，需要跨线程使用连接
        )
        # row_factory=Row：查询结果既能按位置取值，也能像字典一样按列名取值，
        # 例如 row["status"]，可读性比 row[2] 好得多。
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        # 打开外键约束（SQLite 默认是关闭的！）
        conn.execute("PRAGMA foreign_keys=ON")
        # NORMAL 在 WAL 模式下兼顾安全与速度：断电最多丢最近少量事务，
        # 不会像 OFF 那样有数据库损坏风险。
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # contextmanager 让调用方可以写 `with db.connect() as conn:`
        # 无论中间是否抛异常，finally 都会关闭连接，避免连接泄漏。
        if not self._initialized:
            # 便宜且幂等的保护：临时脚本/测试没调用 init() 也能用。
            self.init()
        conn = self._raw_connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction (BEGIN IMMEDIATE) with a short lock retry."""
        # 为什么用 BEGIN IMMEDIATE 而不是普通 BEGIN？
        #   SQLite 的普通事务默认延迟加写锁，可能在提交时才冲突；
        #   IMMEDIATE 一开始就拿写锁，把“抢锁失败”提前到入口，
        #   再配几次短暂重试，能显著减少并发写入时的随机失败。
        if not self._initialized:
            self.init()
        conn = self._raw_connect()
        try:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    last_error = None
                    break
                except sqlite3.OperationalError as exc:
                    last_error = exc
                    # 只有“锁住/忙”才重试；其他错误立即抛出。
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    time.sleep(0.05 * (attempt + 1))  # 50ms、100ms、150ms 退避
            if last_error is not None:
                raise last_error
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                # BaseException 包括 KeyboardInterrupt，确保 Ctrl+C 时也回滚。
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        # 只读查询不需要显示事务；同一个函数名让调用方语义更清楚。
        conn = self._raw_connect()
        try:
            yield conn
        finally:
            conn.close()

    # -- convenience ----------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> int:
        # 自动开事务并返回受影响行数，适合“一句话写入”。
        with self.tx() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.read() as conn:
            return list(conn.execute(sql, params).fetchall())

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple = (), default=None):
        # 取第一行第一列，常用于 SELECT COUNT(*) / MAX(...)。
        row = self.fetchone(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    def backup_to(self, destination: str | Path) -> Path:
        # 用 SQLite 官方的在线备份 API，而不是直接复制 .sqlite3 文件。
        # 为什么？WAL 模式下还有 -wal/-shm 文件，直接复制可能拿到不一致的中间状态。
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            # 先把 WAL 里的改动合并回主库文件。
            source.execute("PRAGMA wal_checkpoint(FULL)")
            target = sqlite3.connect(str(destination))
            try:
                source.backup(target)
            finally:
                target.close()
        return destination


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    # sqlite3.Row 不是真正的 dict，需要显式转换后才能 json.dumps。
    return dict(row) if row is not None else None


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    # settings 表是简单的键值对，用来存“下次采集时间”“预算暂停”等运行状态。
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def get_bool_setting(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    # 数据库里布尔值统一存成文本；这里把常见写法都认成 True。
    value = get_setting(conn, key, "1" if default else "0")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def set_setting(conn: sqlite3.Connection, key: str, value: str, at: str | None = None) -> None:
    # UPSERT：没有就 INSERT，有就 UPDATE。
    # excluded.value 指“这次想插入的新值”。
    stamp = at or utcnow_iso()
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, stamp),
    )


def json_dumps(value: Any) -> str:
    # 项目里存 JSON 的统一格式：
    #   ensure_ascii=False -> 中文保持中文，不变成 \uXXXX
    #   sort_keys=True     -> 同样的内容总是同样的字符串，方便哈希/比对
    #   separators         -> 去掉空格，存得更紧凑
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default=None):
    # 不抛异常的 JSON 解析：数据库里可能是空串、坏数据，
    # 读不出来时返回默认值，避免整个查询流程崩掉。
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
