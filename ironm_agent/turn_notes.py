"""每轮查到和做了的（spec [2026-10-23] §254 需求 2）：作答时工具查到的东西、动作做成的结果留下来，
跟着那一轮带给后面几轮，后面能直接引用、不用重新查。

只本人本会话用；和 mirobody 对话内容同一种加密；每个会话只留最近 KEEP 轮的；注销账号时随 ironm_ 表一起删。
翻聊天、装规程、查 App 用法这类不留（不是查到的事实，下一轮也用不上）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ironm_agent.ddl import run_ddl

_log = logging.getLogger("ironm_agent.turn_notes")

KEEP = 6  # 每个会话留最近几轮的（带给模型的原文最多 4 轮，多留两轮备用）
CALL_MAX = 2000  # 一次工具结果最多留多少字（存原文，只防异常长的，§260）
TURN_MAX = 8000  # 一轮最多留多少字
SHORT_MIN = 800  # 一轮查到的超过这么多字，回答后在后台另写一份要点（§260）
SHORT_MAX = 300
SKIP = frozenset({"read_chat", "load_skill", "app_help", "last_sources"})
_IDS = re.compile(r"〔[^〕]*〕")  # 那一轮的出处编号（C、r 开头的是那一轮自己的，下一轮重新登记）


def _brief(args: Mapping[str, Any]) -> str:
    if not args:
        return ""
    query = args.get("query")
    text = str(query) if isinstance(query, str) and len(args) == 1 else json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 80 else text[:80] + "…"


def _clip(text: str, cap: int) -> str:
    text = " ".join(_IDS.sub("", text).split())
    return text if len(text) <= cap else text[:cap] + "…"


def notes_text(said: Sequence[tuple[str, str, Mapping[str, Any], str]]) -> str:
    """这一轮的工具结果拼成几行：哪一步（查了什么）：结果。没有要留的返回空。"""
    lines: list[str] = []
    left = TURN_MAX
    for name, title, args, result in said:
        if name in SKIP or not str(result).strip():
            continue
        what = _brief(args)
        line = f"- {title}" + (f"（{what}）" if what else "") + "：" + _clip(str(result), CALL_MAX)
        if len(line) > left:
            line = line[:left] + "…"
        lines.append(line)
        left -= len(line)
        if left <= 0:
            break
    return "\n".join(lines)


class NotesStore(Protocol):
    async def get(self, user_id: str, session: str, turns: Sequence[str]) -> dict[str, tuple[str, str]]: ...
    async def put(self, user_id: str, session: str, turn: str, notes: str, short: str = "") -> None: ...


class NotesBook:
    """内存版（测试用）。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[tuple[str, str, str]]] = {}

    async def get(self, user_id: str, session: str, turns: Sequence[str]) -> dict[str, tuple[str, str]]:
        return {t: (n, sh) for t, n, sh in self.rows.get((user_id, session), []) if t in set(turns)}

    async def put(self, user_id: str, session: str, turn: str, notes: str, short: str = "") -> None:
        rows = [x for x in self.rows.get((user_id, session), []) if x[0] != turn]
        self.rows[(user_id, session)] = [*rows, (turn, notes, short)][-KEEP:]


DDL = """
CREATE TABLE IF NOT EXISTS ironm_turn_notes (
    user_id    text NOT NULL,
    session    text NOT NULL,
    turn       text NOT NULL,
    notes      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session, turn)
)
"""
INDEX = "CREATE INDEX IF NOT EXISTS ironm_turn_notes_session ON ironm_turn_notes (user_id, session, created_at)"
SHORT_COLUMN = "ALTER TABLE ironm_turn_notes ADD COLUMN IF NOT EXISTS short text"  # 要点（§260），加密


class PgNotes:
    """数据库版：和 mirobody 对话内容同一种加密。"""

    def __init__(self) -> None:
        self._ready = False

    async def _q(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from mirobody.utils.db import execute_query

        if not self._ready:
            await run_ddl(DDL, INDEX, SHORT_COLUMN)
            self._ready = True
        rows = await execute_query(sql, params, log_sql=False)
        return rows if isinstance(rows, list) else []

    async def get(self, user_id: str, session: str, turns: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not turns:
            return {}
        got = await self._q(
            "SELECT turn, decrypt_content(notes) AS notes,"
            " CASE WHEN short IS NULL THEN '' ELSE decrypt_content(short) END AS short FROM ironm_turn_notes"
            " WHERE user_id = :uid AND session = :s AND turn = ANY(:turns)",
            {"uid": user_id, "s": session, "turns": list(turns)},
        )
        return {str(r["turn"]): (str(r.get("notes") or ""), str(r.get("short") or "")) for r in got}

    async def put(self, user_id: str, session: str, turn: str, notes: str, short: str = "") -> None:
        await self._q(
            "INSERT INTO ironm_turn_notes (user_id, session, turn, notes, short)"
            " VALUES (:uid, :s, :t, encrypt_content(:n), CASE WHEN :sh = '' THEN NULL ELSE encrypt_content(:sh) END)"
            " ON CONFLICT (user_id, session, turn) DO UPDATE SET notes = EXCLUDED.notes, short = EXCLUDED.short",
            {"uid": user_id, "s": session, "t": turn, "n": notes, "sh": short},
        )
        await self._q(  # 每个会话只留最近 KEEP 轮的
            "DELETE FROM ironm_turn_notes WHERE user_id = :uid AND session = :s AND turn NOT IN ("
            " SELECT turn FROM ironm_turn_notes WHERE user_id = :uid AND session = :s"
            " ORDER BY created_at DESC LIMIT :k) RETURNING 1",
            {"uid": user_id, "s": session, "k": KEEP},
        )


_default: NotesStore | None = None


def default_notes() -> NotesStore:
    global _default
    if _default is None:
        _default = PgNotes()
    return _default


SHORT_PROMPT = """把这一轮健身教练查到和做了的记录压成要点，下一轮接着用。保留所有食物名、动作名、数字和单位，
去掉重复和说明性的话；不加推测；不超过 300 字。只输出要点本身。"""


async def shorten(llm: Any, notes: str) -> str:
    """一轮查到的太长时写一份要点（回答后在后台，§260）；模型不可用时返回空，用原文的开头代替由调用方决定。"""
    from ironm_agent.llm import ModelUnavailable

    if len(notes) <= SHORT_MIN:
        return ""
    try:
        reply = await llm.chat("memory", [{"role": "system", "content": SHORT_PROMPT}, {"role": "user", "content": notes}])
    except ModelUnavailable:
        return ""
    text = " ".join(str(reply.content or "").split())
    return text[:SHORT_MAX]
