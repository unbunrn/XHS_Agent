"""Append-only audit trail helpers."""

# 中文说明：
# 这个文件提供 audit()，往 audit_log 表里追加一条“谁在什么时候做了什么”。
# 审计日志就像银行流水：只追加、不修改、不删除，出问题时可回溯。
# 它不参与业务判断，纯粹是记录，所以即使写日志失败通常也不该改变主流程
# （调用方一般在同一个数据库事务里写，保证“事情发生”和“有记录”一起成立）。

from __future__ import annotations

import sqlite3
from typing import Any

from .db import json_dumps
from .timeutil import utcnow_iso


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    *,
    entity: str | None = None,
    entity_id: str | int | None = None,
    detail: Any = None,
    at: str | None = None,
) -> None:
    # 参数说明：
    #   conn      调用方传进来的数据库连接。这里不自己开事务，
    #             这样审计记录能和业务写入在同一个事务里提交或回滚。
    #   actor     动作发起者，例如 owner 的 open_id、worker、publish_gate。
    #   action    动作名，例如 approval.approved、publish.claimed。
    #   entity    实体类型（approval/draft/event/job……）。
    #   entity_id 实体编号；统一转成字符串，避免不同类型比较时出问题。
    #   detail    任意可 JSON 序列化的补充信息（密钥绝不能放进来）。
    #   at        时间；不传就用当前 UTC 时间。
    #
    # 注意 json_dumps 使用 sort_keys=True，所以同样的内容总是序列化成
    # 同样的字符串，方便比对和测试。
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, entity, entity_id, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            at or utcnow_iso(),
            actor,
            action,
            entity,
            None if entity_id is None else str(entity_id),
            json_dumps(detail) if detail is not None else None,
        ),
    )
