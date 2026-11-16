"""会话内的短期记忆：从 mirobody 的对话记录里读本会话最近几轮，只用来理解指代（"上面说的"）。

带用户最近几句话，加上最近两条回答的要点（开头一小段），不带整条回答，免得把旧话题带进新问题。
跨会话的长期记忆见 memory_store / memory_view。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

_log = logging.getLogger("ironm_agent.memory")

TURNS = 10  # 用户最近几句话（spec [2026-10-23] §53）
ANSWERS = 4  # 最近几条回答的要点
ANSWER_CHARS = 150  # 每条回答取开头多少字作为要点
LAST_CHARS = 800  # 上一条回答完整给出，最多这么多字（追问"你刚才第 3 条"时对得上）
USER = "用户："
COACH = "教练（要点）："
SAID_CHARS = 200  # 前几轮里他的话最多带多少字；截短的末尾带"…"，原文用 read_chat 查（§207）


def _cut(said: str) -> str:
    said = said.strip()
    return said if len(said) <= SAID_CHARS else said[:SAID_CHARS] + "…"


def split_thread(session_id: str) -> tuple[str, str]:
    """mirobody 传进来的会话编号是"提问人:会话"，拆成两段。"""
    owner, _, session = session_id.partition(":")
    return (owner, session) if session else ("", session_id)


def _gist(answer: str) -> str:
    """回答的要点：开头一小段（第一段里最多 ANSWER_CHARS 个字）。"""
    first = answer.strip().split("\n", 1)[0].strip()
    return first if len(first) <= ANSWER_CHARS else first[:ANSWER_CHARS] + "…"


def _whole(answer: str) -> str:
    """上一条回答：全文（换行压成空格），最多 LAST_CHARS 个字。"""
    text = " ".join(x.strip() for x in answer.strip().split("\n") if x.strip())
    return text if len(text) <= LAST_CHARS else text[:LAST_CHARS] + "…"


def thread_lines(
    rows: list[dict[str, Any]], current: str, turns: int = TURNS, keep: Sequence[str] = ()
) -> list[str]:
    """从对话记录里排出最近几轮：用户的原话，加上最近 ANSWERS 条回答的要点，上一条回答给全文。
    含 keep 里任一标志的回答（列给他确认的条件、预览说明）给全文，也不受 ANSWERS 条的限制（§68）。"""
    lines: list[tuple[str, str]] = []
    for row in rows:
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if row.get("role") == "user":
            lines.append(("user", _cut(content)))
        elif row.get("role") == "assistant":
            lines.append(("coach", content))
    if lines and lines[-1] == ("user", _cut(current)):
        lines.pop()  # 本轮的问题已经先存进去了
    users = [i for i, (who, _) in enumerate(lines) if who == "user"]
    if not users:
        return []
    kept = lines[users[-turns] if len(users) >= turns else users[0] :]
    coach = [i for i, (who, _) in enumerate(kept) if who == "coach"]
    key = {i for i in coach if any(m in kept[i][1] for m in keep)}
    shown = set(coach[-ANSWERS:]) | key
    last = coach[-1] if coach else -1
    return [
        USER + text
        if who == "user"
        else COACH + (_whole(text) if i == last or i in key else _gist(text))
        for i, (who, text) in enumerate(kept)
        if who == "user" or i in shown
    ]


async def recent(
    session_id: str, current: str, turns: int = TURNS, keep: Sequence[str] = ()
) -> list[str]:
    """本会话最近几轮（用户原话、最近几条回答的要点、上一条回答全文）；读不到时返回空列表。"""
    owner, session = split_thread(session_id)
    if not owner or not session:
        return []
    try:
        from mirobody.agent.chat.message import get_chat_history

        rows: list[dict[str, Any]] = await get_chat_history(owner, session)
    except Exception as e:  # 读不到历史不影响作答
        _log.warning("history unavailable: error_type=%s", type(e).__name__)
        return []
    return thread_lines(rows, current, turns, keep)


async def earlier(session_id: str) -> str:
    """本会话之前的全部回答原文（只用来判断某条固定提醒说过没有，不交给模型）；读不到时为空。"""
    owner, session = split_thread(session_id)
    if not owner or not session:
        return ""
    try:
        from mirobody.agent.chat.message import get_chat_history

        rows: list[dict[str, Any]] = await get_chat_history(owner, session)
    except Exception as e:
        _log.warning("history unavailable: error_type=%s", type(e).__name__)
        return ""
    return "\n".join(str(r.get("content") or "") for r in rows if r.get("role") == "assistant")


def user_lines(history: list[str]) -> list[str]:
    """只要用户自己说的话（记忆提取用：记忆只能来自用户的原话）。"""
    return [line for line in history if line.startswith(USER)]


async def last_sources(session_id: str, tool_call_id: str) -> str | None:
    """本会话上一条回答的“依据与来源”（存在 mirobody 的对话记录里）；没有时返回 None。"""
    owner, session = split_thread(session_id)
    if not owner or not session:
        return None
    try:
        from mirobody.agent.chat.message import get_chat_history

        rows: list[dict[str, Any]] = await get_chat_history(owner, session)
    except Exception as e:
        _log.warning("history unavailable: error_type=%s", type(e).__name__)
        return None
    from ironm_agent.provenance import is_explanation

    for row in reversed(rows):
        if row.get("role") == "user":
            continue
        if is_explanation(str(row.get("content") or "")):
            continue  # 上一次追问来源的回复，往前找真正的回答
        for block in row.get("content_dict") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_call_id") == tool_call_id
            ):
                return str(block.get("content") or "")
        return ""  # 上一条回答没有附来源
    return None


def transcript(rows: list[dict[str, Any]], each: int = 300) -> list[str]:
    """整个会话的对话（写会话摘要用）：用户原话和回答，每句最多 each 个字。"""
    out = []
    for r in rows:
        content = r.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        who = USER if r.get("role") == "user" else "教练：" if r.get("role") == "assistant" else ""
        if who:
            text = " ".join(content.split())
            out.append(who + (text if len(text) <= each else text[:each] + "…"))
    return out


async def whole(session_id: str) -> list[str]:
    """本会话全部对话（会话摘要用）；读不到时为空。"""
    owner, session = split_thread(session_id)
    if not owner or not session:
        return []
    try:
        from mirobody.agent.chat.message import get_chat_history

        rows: list[dict[str, Any]] = await get_chat_history(owner, session)
    except Exception as e:
        _log.warning("history unavailable: error_type=%s", type(e).__name__)
        return []
    return transcript(rows)


# ---- 翻本会话原文（spec [2026-10-23] §207）----------------------------------------------------
# 每轮带的前几轮是截短的（他的话 200 字、旧回答只有要点）；管家需要时用这个工具查原文，不把原文常驻进提示词。

READ_CHAT = "read_chat"
CHAT_CHARS = 3000  # 一次最多返回多少字
LINE_CHARS = 1500  # 一句最多多少字（关键词在后半段时取它前后）
CHAT_TURNS = 3  # 不写关键词时给最近几轮
NOT_FOUND = "这次聊天里没有查到。"
UNREADABLE = "这次聊天的记录现在读不到。"
READ_CHAT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": READ_CHAT,
        "description": "查这次聊天里前面说过的原文。上面带的前几轮只有开头：他的话末尾带「…」的没列全，你早先的回答只有要点。"
        "他提到前面贴过的内容（化验单、计划、数据）、或问你早先回答里的细节而上面看不全时用。只查这一次聊天，更早的事用 recall_memory。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "description": "关键词，如 化验单、深蹲；空字符串看最近几轮"},
                "turns": {"type": "integer", "description": "不写关键词时看最近几轮，默认 3"},
            },
            "required": ["query"],
        },
    },
}


def _window(text: str, at: int) -> str:
    """一句太长时取关键词前后的一段。"""
    if len(text) <= LINE_CHARS:
        return text
    start = max(0, min(at - LINE_CHARS // 3, len(text) - LINE_CHARS))
    return ("…" if start else "") + text[start : start + LINE_CHARS] + ("…" if start + LINE_CHARS < len(text) else "")


def chat_search(rows: Sequence[Mapping[str, Any]], query: str, turns: int, current: str) -> str:
    """本会话前面的原文：有关键词就找带这个词的话（他说的、你答的都算），没有就给最近几轮；按先后排，
    总共不超过 CHAT_CHARS 字（超了留最近的）。这一轮的问题不算在内。"""
    from ironm_agent.memory_extract import norm

    said: list[tuple[str, str]] = []
    for r in rows:
        content = r.get("content")
        who = USER if r.get("role") == "user" else "教练：" if r.get("role") == "assistant" else ""
        if who and isinstance(content, str) and content.strip():
            said.append((who, " ".join(content.split())))
    if said and said[-1] == (USER, " ".join(current.split())):
        said.pop()  # 本轮的问题已经先存进去了
    q = norm(query)
    if q:
        picked = [(who, _window(text, max(0, text.lower().find(query.strip().lower()))))
                  for who, text in said if q in norm(text)]  # fmt: skip
    else:
        users = [i for i, (who, _) in enumerate(said) if who == USER]
        n = max(1, min(turns, 10))
        picked = [(who, _window(text, 0)) for who, text in (said[users[-n] if len(users) >= n else 0 :] if users else [])]
    out: list[str] = []
    left = CHAT_CHARS
    for who, text in reversed(picked):  # 超了留最近的
        if left <= 0:
            break
        line = who + (text if len(text) <= left else text[:left] + "…")
        out.append(line)
        left -= len(line)
    return "\n".join(reversed(out)) or NOT_FOUND


def chat_tool(
    session_id: str,
    current: str,
    load: Callable[[str, str], Awaitable[Sequence[Mapping[str, Any]]]] | None = None,
) -> Any:
    """查本会话原文的工具：只读这个人、这一次聊天（会话编号里带着是谁的）；读不到时说读不到。"""

    async def run(box: Any, args: Mapping[str, Any]) -> str:
        owner, session = split_thread(session_id)
        if not owner or not session:
            return UNREADABLE
        try:
            if load is None:
                from mirobody.agent.chat.message import get_chat_history

                rows: Sequence[Mapping[str, Any]] = await get_chat_history(owner, session)
            else:
                rows = await load(owner, session)
        except Exception as e:
            _log.warning("history unavailable: error_type=%s", type(e).__name__)
            return UNREADABLE
        try:
            turns = int(args.get("turns") or CHAT_TURNS)
        except (TypeError, ValueError):
            turns = CHAT_TURNS
        return chat_search(rows, str(args.get("query") or ""), turns, current)

    return run
