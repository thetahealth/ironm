"""消息的接口（spec [2026-10-23] §235）：管家消息和系统消息的列表、没看过的有几条、标成看过。

管家消息读已有的调整经历，不改它、不调用模型；看未读数不触发管家重新评估（只读存着的方案）。
只读写本人的；日志不记内容。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ironm_agent.notices import NoticeStore, coach_messages, default_notices, unread_coach
from ironm_agent.web import _verify

_log = logging.getLogger("ironm_agent.notices")
router: APIRouter = APIRouter()
COACH_HISTORY = 400  # 管家的调整经历一次最多读多少条（够 180 天）
PAGE = 100  # 一次列多少条


def notice_store() -> NoticeStore:
    return default_notices()


def coach_store() -> Any:
    from ironm_agent.web_more import coach_deps

    return coach_deps().store


NOTICES: Any = Depends(notice_store)
COACH: Any = Depends(coach_store)


async def _coach(coach: Any, user_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(管家消息, 还等他确认的)；读不到当作没有，系统消息照常看。"""
    from ironm_agent.coach_state import pending

    try:
        history = await coach.history(user_id, COACH_HISTORY)
        state = await coach.get(user_id)
    except Exception as e:
        _log.warning("coach messages unavailable: error_type=%s", type(e).__name__)
        return [], []
    waiting = [x.model_dump(mode="json") for x in pending(state, date.today())] if state is not None else []
    return coach_messages(history, undoable(state, date.today())), waiting


def undoable(state: Any, today: date) -> list[str]:
    """现在还撤得回去的调整（和"不用这样调"的规则同一套，§85、§182）：当天复盘做的、前几天提的还挂着的；
    只是说一句、问一句的不算。真撤的时候接口还会再核一遍。"""
    from ironm_agent.coach_state import carry

    if state is None:
        return []
    review = state.review if state.review is not None and state.review.day == today else None
    now = [d for d in review.done if not d.undone] if review is not None else []
    return [d.key for d in [*now, *carry(state, today)] if d.type not in ("note", "ask")]


def _day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "日期写法不对，要写成 2026-11-18") from None


def _local_day(at: datetime | str) -> date:
    """按服务器的本地日期算哪天（§238）。"""
    when = datetime.fromisoformat(at) if isinstance(at, str) else at
    return when.astimezone().date()


Q_MAX = 20  # 关键字最多几个字（§239）
FOUND_MAX = 200  # 找的时候最多列几条


def _hit(q: str, *texts: Any) -> bool:
    """关键字在不在这些字里（不分大小写）。"""
    return any(q in str(t).lower() for t in texts if t)


@router.get("/iron/api/messages")
async def list_messages(
    day: str | None = None,
    q: str | None = None,
    user_id: str = Depends(_verify()),
    notices: NoticeStore = NOTICES,
    coach: Any = COACH,
) -> dict[str, Any]:
    """两个页签的内容（§238）：默认只给今天和昨天的；day 写了就只给那一天的。等确认的不管哪天提的都给。
    q 是关键字（§239）：写了就在全部日子里找（和 day 一起用就在那一天里找），最多 200 条。
    每个页签带上没看过的有几条、上次看到什么时候（页面按它给圆点上色）。"""
    want = _day(day)
    word = " ".join(str(q or "").split())[:Q_MAX].lower()
    today = date.today()
    days = [want] if want is not None else [today, date.fromordinal(today.toordinal() - 1)]
    messages, waiting = await _coach(coach, user_id)
    every = await notices.list(user_id, PAGE * 5)  # 180 天里的系统消息，够按天挑
    seen_coach, seen_system = await notices.seen(user_id, "coach"), await notices.seen(user_id, "system")

    def on_day(at: Any) -> bool:
        return (want is None and bool(word)) or _local_day(at) in days  # 只写关键字：不限日子

    coach_items = [m for m in messages if on_day(m["at"]) and (not word or _hit(
        word, m["title"], m["why"], m["before"], m["expect"], m["outcome"], *(m.get("lines") or ())))][:FOUND_MAX]  # fmt: skip
    system_items = [n for n in every if on_day(n.at) and (not word or _hit(word, n.title, n.text))][:FOUND_MAX]
    return {
        "days": [d.isoformat() for d in days], "q": word,
        "coach": {
            "items": coach_items, "pending": waiting,
            "unread": unread_coach(messages, seen_coach, len(waiting)),
            "seen": seen_coach.isoformat() if seen_coach else None,
        },
        "system": {
            "items": [n.model_dump(mode="json") for n in system_items],
            "unread": sum(1 for n in every if seen_system is None or n.at > seen_system),
            "seen": seen_system.isoformat() if seen_system else None,
        },
    }  # fmt: skip


@router.get("/iron/api/messages/unread")
async def unread_messages(
    user_id: str = Depends(_verify()), notices: NoticeStore = NOTICES, coach: Any = COACH
) -> dict[str, int]:
    """铃铛上的数字。"""
    messages, waiting = await _coach(coach, user_id)
    seen_system = await notices.seen(user_id, "system")
    c = unread_coach(messages, await notices.seen(user_id, "coach"), len(waiting))
    s = sum(1 for n in await notices.list(user_id, PAGE) if seen_system is None or n.at > seen_system)
    return {"coach": c, "system": s, "total": c + s}


class SeenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tab: Literal["coach", "system"]


@router.post("/iron/api/messages/seen")
async def seen_messages(
    body: SeenIn, user_id: str = Depends(_verify()), notices: NoticeStore = NOTICES
) -> dict[str, Any]:
    """打开了哪个页签：那个页签算看过（要确认的调整照样算在数字里，直到他点了）。"""
    await notices.mark_seen(user_id, body.tab, datetime.now(UTC))
    return {"tab": body.tab, "seen": True}
