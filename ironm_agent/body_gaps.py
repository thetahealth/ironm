"""没覆盖到的记下来（spec [2026-10-23] §265 需求 8）：读身体情况时异常但归不进身体情况的、报告里认不出的结论、
查不到的医学资料、模型写了但不在身体情况里的词；被安全底线退回的记在运行记录里。定期按次数看，决定补哪些。

只记指标名、结论原话、查询词这类，不记数值和日期；和 mirobody 对话内容同一种加密；注销账号时随 ironm_ 表一起删。
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ironm_agent.ddl import run_ddl

_log = logging.getLogger("ironm_agent.body")

KINDS = {"unmapped": "归不进身体情况", "unknown": "认不出的结论", "not_found": "查不到资料", "states": "不在身体情况里的词"}


def rows_of(gaps: Any) -> list[tuple[str, str]]:
    return [(k, str(v)) for k in KINDS for v in getattr(gaps, k, ()) or ()]


class GapStore(Protocol):
    async def add(self, user_id: str, gaps: Any) -> None: ...
    async def counts(self) -> Counter[tuple[str, str]]: ...


@dataclass
class GapBook:
    """内存版（测试用）。"""

    rows: list[tuple[str, str, str, datetime]] = field(default_factory=list)

    async def add(self, user_id: str, gaps: Any) -> None:
        now = datetime.now().astimezone()
        self.rows += [(user_id, k, v, now) for k, v in rows_of(gaps)]

    async def counts(self) -> Counter[tuple[str, str]]:
        return Counter((k, v) for _, k, v, _ in self.rows)


DDL = """
CREATE TABLE IF NOT EXISTS ironm_body_gap (
    user_id    text NOT NULL,
    kind       text NOT NULL,
    item       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""


class PgGaps:
    def __init__(self) -> None:
        self._ready = False

    async def _q(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from mirobody.utils.db import execute_query

        if not self._ready:
            await run_ddl(DDL)
            self._ready = True
        rows = await execute_query(sql, params, log_sql=False)
        return rows if isinstance(rows, list) else []

    async def add(self, user_id: str, gaps: Any) -> None:
        for kind, item in rows_of(gaps):
            await self._q("INSERT INTO ironm_body_gap (user_id, kind, item) VALUES (:uid, :k, encrypt_content(:i)) RETURNING 1",
                          {"uid": str(user_id), "k": kind, "i": item})  # fmt: skip

    async def counts(self) -> Counter[tuple[str, str]]:
        got = await self._q("SELECT kind, decrypt_content(item) AS item FROM ironm_body_gap", {})
        return Counter((str(r["kind"]), str(r["item"])) for r in got)


_default: GapStore | None = None


def default_gaps() -> GapStore:
    global _default
    if _default is None:
        _default = PgGaps()
    return _default
