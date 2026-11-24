"""消息（spec [2026-10-23] §235）：系统消息的存储，和把管家的调整经历整理成给人看的消息。

系统消息：后台任务有了结果就写一条（视频转文字、视频出方案、管家当天没看成），只属于本人、内容加密、
保留 180 天；写失败不影响那个任务本身。日志只记种类，不记内容。
管家消息不另存：读 coach_state 里已有的调整经历，在这里排成一条条消息——他点的确认、暂不记在原来那条上，
同一天几个动作的下次目标合成一条。只读，不调用模型。
每个页签上次看到什么时候也存在这里，用来算没看过的有几条。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ironm_agent.ddl import run_ddl

_log = logging.getLogger("ironm_agent.notices")

RETAIN_DAYS = 180
type Level = Literal["info", "warn"]


class Notice(BaseModel):
    """一条系统消息。link 是点了去哪（页面里的地址，如 #atlas/plans），没有就只是看。"""

    id: str = ""
    kind: str = Field(min_length=1, max_length=30)  # video_text / video_plan / coach_failed …
    title: str = Field(min_length=1, max_length=40)
    text: str = Field(default="", max_length=200)
    level: Level = "info"
    link: str = Field(default="", max_length=80)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NoticeStore(Protocol):
    async def add(self, user_id: str, notice: Notice) -> bool: ...
    async def list(self, user_id: str, limit: int = 100, before: datetime | None = None) -> list[Notice]: ...
    async def seen(self, user_id: str, tab: str) -> datetime | None: ...
    async def mark_seen(self, user_id: str, tab: str, at: datetime) -> None: ...


class NoticeBook:
    """内存版（测试与自测用）。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Notice]] = {}
        self.seen_at: dict[tuple[str, str], datetime] = {}

    async def add(self, user_id: str, notice: Notice) -> bool:
        mine = self.rows.setdefault(user_id, {})
        cut = notice.at - timedelta(days=RETAIN_DAYS)
        for k in [k for k, v in mine.items() if v.at < cut]:
            del mine[k]
        if notice.id in mine:  # 同一个编号再写：不重复（管家当天没看成只记一条）
            return False
        mine[notice.id] = notice
        return True

    async def list(self, user_id: str, limit: int = 100, before: datetime | None = None) -> list[Notice]:
        rows = sorted(self.rows.get(user_id, {}).values(), key=lambda n: n.at, reverse=True)
        return [n for n in rows if before is None or n.at < before][:limit]

    async def seen(self, user_id: str, tab: str) -> datetime | None:
        return self.seen_at.get((user_id, tab))

    async def mark_seen(self, user_id: str, tab: str, at: datetime) -> None:
        self.seen_at[(user_id, tab)] = at


DDL = (
    """
CREATE TABLE IF NOT EXISTS ironm_notice (
    id         text NOT NULL,
    user_id    text NOT NULL,
    content    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, id)
)
""",
    "CREATE INDEX IF NOT EXISTS ironm_notice_user ON ironm_notice (user_id, created_at)",
    """
CREATE TABLE IF NOT EXISTS ironm_notice_seen (
    user_id text NOT NULL,
    tab     text NOT NULL,
    seen_at timestamptz NOT NULL,
    PRIMARY KEY (user_id, tab)
)
""",
)


class PgNotices:
    """数据库版：消息内容用 mirobody 的加密函数加密。"""

    def __init__(self) -> None:
        self._ready = False
        self._purged: Any = None

    async def _q(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from mirobody.utils.db import execute_query

        if not self._ready:
            await run_ddl(*DDL)
            self._ready = True
        rows = await execute_query(sql, params, log_sql=False)
        return rows if isinstance(rows, list) else []

    async def add(self, user_id: str, notice: Notice) -> bool:
        rows = await self._q(
            "INSERT INTO ironm_notice (id, user_id, content, created_at)"
            " VALUES (:id, :uid, encrypt_content(:c), :at) ON CONFLICT (user_id, id) DO NOTHING RETURNING id",
            {"id": notice.id, "uid": user_id, "c": notice.model_dump_json(), "at": notice.at},
        )
        today = notice.at.date()
        if self._purged != today:  # 每天清一次过期的
            self._purged = today
            await self._q(
                "DELETE FROM ironm_notice WHERE created_at < :cut",
                {"cut": notice.at - timedelta(days=RETAIN_DAYS)},
            )
        return bool(rows)

    async def list(self, user_id: str, limit: int = 100, before: datetime | None = None) -> list[Notice]:
        rows = await self._q(
            "SELECT decrypt_content(content) AS content FROM ironm_notice WHERE user_id = :uid"
            + (" AND created_at < :before" if before is not None else "")
            + " ORDER BY created_at DESC LIMIT :n",
            {"uid": user_id, "n": limit, **({"before": before} if before is not None else {})},
        )
        return [Notice.model_validate_json(r["content"]) for r in rows]

    async def seen(self, user_id: str, tab: str) -> datetime | None:
        rows = await self._q(
            "SELECT seen_at FROM ironm_notice_seen WHERE user_id = :uid AND tab = :tab",
            {"uid": user_id, "tab": tab},
        )
        return rows[0]["seen_at"] if rows else None

    async def mark_seen(self, user_id: str, tab: str, at: datetime) -> None:
        await self._q(
            "INSERT INTO ironm_notice_seen (user_id, tab, seen_at) VALUES (:uid, :tab, :at)"
            " ON CONFLICT (user_id, tab) DO UPDATE SET seen_at = EXCLUDED.seen_at",
            {"uid": user_id, "tab": tab, "at": at},
        )


_default: NoticeStore | None = None


def default_notices() -> NoticeStore:
    global _default
    if _default is None:
        _default = PgNotices()
    return _default


async def notify(
    user_id: str,
    kind: str,
    title: str,
    text: str = "",
    *,
    level: Level = "info",
    link: str = "",
    key: str = "",
    store: NoticeStore | None = None,
) -> bool:
    """写一条系统消息。key 相同的只记一条（如"管家当天没看成"按日期给 key）。
    写不成只记日志，不抛出：消息是附带的，不能连累那个后台任务。"""
    try:
        notice = Notice(id=key or uuid.uuid4().hex[:16], kind=kind, title=title[:40], text=text[:200],
                        level=level, link=link)  # fmt: skip
        added = await (store or default_notices()).add(user_id, notice)
        _log.info("notice: kind=%s added=%s", kind, added)
        return added
    except Exception as e:
        _log.warning("notice not written: kind=%s error_type=%s", kind, type(e).__name__)
        return False


# ---- 管家消息：把调整经历排成一条条消息 ---------------------------------------------------------------

_CHOICE = {"accepted": "accept", "declined": "later"}
def coach_messages(history: Sequence[Any], undoable: Sequence[str] = ()) -> list[dict[str, Any]]:
    """调整经历（coach_state.Change，顺序不限）→ 给人看的消息，新的在前。
    他点的确认、暂不记在原来那条上（decided：结果和时间），找不到原来那条才单独成一条；
    同一天几个动作的下次目标合成一条（lines 里列每个动作）。
    每条带上 undo：现在还撤不撤得回去（§236、§240）——undoable 是后端说还能撤的编号，
    同一个编号只有最新那条、而且他还没点过的才给。"""
    rows: list[dict[str, Any]] = []
    open_by_key: dict[str, dict[str, Any]] = {}  # 每个编号最近一条还没记上选择的
    targets: dict[Any, dict[str, Any]] = {}  # 哪天 → 那天的"下次目标"那一条
    for c in sorted(history, key=lambda c: c.at):
        if c.change in _CHOICE:
            said = {"decision": _CHOICE[c.change], "at": c.at.isoformat()}
            row = open_by_key.pop(c.key, None)
            if row is not None:
                if c.change == "declined" and row["status"] == "auto":
                    said["decision"] = "undo"  # 自动做的被他撤了，不是"暂不"
                row["decided"] = said
                continue
            rows.append({**_row(c), "decided": said})
            continue
        if c.kind == "target":
            row = targets.get(c.day)
            if row is None:
                row = targets[c.day] = {**_row(c), "kind": "targets", "lines": [], "why": "", "before": ""}
                rows.append(row)
            line = c.after if c.change != "removed" else f"{c.before}（已解除）"
            row["lines"].append(f"{line}（{c.why}）" if c.why and c.change != "removed" else line)  # 每个动作带它自己的理由（§237）
            row["at"] = c.at.isoformat()
            row["title"] = f"定了 {len(row['lines'])} 个动作的下次目标"
            continue
        row = _row(c)
        rows.append(row)
        open_by_key[c.key] = row
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:  # 每个编号最新的那一条
        if row["key"] and (row["key"] not in latest or row["at"] > latest[row["key"]]["at"]):
            latest[row["key"]] = row
    for key, row in latest.items():
        said = (row["decided"] or {}).get("decision")
        # 能撤：自动生效的、他还没撤过的；要确认的只在他确认了以后（还没确认的在"等你确认"里给确认、拒绝，不给撤销）
        took = said == "accept" if row["status"] == "confirm" else said is None
        row["undo"] = key in undoable and took
    return sorted(rows, key=lambda r: r["at"], reverse=True)


def _row(c: Any) -> dict[str, Any]:
    removed = c.change == "removed"
    return {
        "id": c.id, "key": c.key, "kind": c.kind, "change": c.change, "status": c.status,
        "at": c.at.isoformat(), "day": c.day.isoformat(),
        "title": f"{c.before}（已解除）" if removed and c.before else (c.after or c.before),
        "before": "" if removed else c.before, "why": c.why,  # 依据编号不给页面（§29：内部留底，追问才说）
        "expect": c.expect, "outcome": c.outcome, "decided": None, "undo": False,
    }  # fmt: skip


def unread_coach(messages: Sequence[dict[str, Any]], seen: datetime | None, pending: int) -> int:
    """管家页签没看过的：上次看过之后管家新做的（要确认的、他自己点的不在这里数），加上还等他确认的几条。"""
    fresh = sum(
        1 for m in messages
        if m["status"] != "confirm" and (seen is None or datetime.fromisoformat(m["at"]) > seen)
    )  # fmt: skip
    return fresh + pending
