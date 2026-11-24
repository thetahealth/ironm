"""会话内的短期记忆：从 mirobody 的对话记录里读本会话最近几轮，只用来理解指代（"上面说的"）。

带用户最近几句话，加上最近两条回答的要点（开头一小段），不带整条回答，免得把旧话题带进新问题。
跨会话的长期记忆见 memory_store / memory_view。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("ironm_agent.memory")

TURNS = 10  # 用户最近几句话（spec [2026-10-23] §53）
ANSWERS = 4  # 最近几条回答的要点
ANSWER_CHARS = 150  # 每条回答取开头多少字作为要点
LAST_CHARS = 6000  # 上一条回答完整给出，只为防异常（§267：不再截到 800 字，结尾的"好些了吗"要在）
USER = "用户："
COACH = "教练（要点）："
SAID_CHARS = 2000  # 前几轮里他的话的防异常上限（§267）；截短的末尾带"…"，原文用 read_chat 查（§207）


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


# ---- 交给作答模型的本会话上下文（spec [2026-10-23] §254）-------------------------------------------
# 最近几轮给原文（一问一答、回答全文），不在一条回答里截头留尾；更早的用会话摘要接上。
# 上面的 thread_lines 只留给代码判断用（确认单、他在回应哪一条），不再交给作答的模型。

CONV_BUDGET = 9000  # 本会话内容一共最多多少字：以内全用原文，超了从最早的起换摘要、要点（§260）
TURNS_MAX = 30  # 最多看最近几轮（再早的一定用摘要）
OPTIONS_MAX = 8  # 最近两天的推荐最多列几条
SAY_MAX = 6000  # 一句他的话的防异常上限（§267：正常长度一律放原文，放不下由篇幅预算换摘要、要点）
ANSWER_MAX = 6000  # 一条回答的防异常上限
CLIPPED = "…（太长，后面没带；原文用 read_chat 查）"
TURN_KEY = "turn"  # 回答开头那一块里存的这一轮编号（plugin 的 TURN_ID）


@dataclass(frozen=True)
class Turn:
    """本会话的一轮：他说的、你答的，加上这一轮的编号（用来找这一轮查到和做了的）。"""

    said: str
    answer: str
    turn: str = ""


def _turn_id(row: Mapping[str, Any]) -> str:
    for block in row.get("content_dict") or []:
        if isinstance(block, dict) and block.get("type") == "tool_call" and block.get("id") == TURN_KEY:
            return str((block.get("args") or {}).get(TURN_KEY) or "")
    return ""


def turns_of(rows: Sequence[Mapping[str, Any]], current: str) -> list[Turn]:
    """对话记录排成一问一答；这一轮的问题（已经先存进去了）不算。"""
    out: list[Turn] = []
    for r in rows:
        content = r.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if r.get("role") == "user":
            out.append(Turn(content.strip(), ""))
        elif r.get("role") == "assistant" and out:
            last = out[-1]
            answer = (last.answer + "\n" + content.strip()).strip()
            out[-1] = Turn(last.said, answer, _turn_id(r) or last.turn)
    if out and not out[-1].answer and " ".join(out[-1].said.split()) == " ".join(current.split()):
        out.pop()
    return out


def turns_from_lines(lines: Sequence[str]) -> list[Turn]:
    """给考题用：注入的是"用户：…""教练（要点）：…"这样的行时，照样排成一问一答。"""
    out: list[Turn] = []
    for line in lines:
        if line.startswith(USER):
            out.append(Turn(line[len(USER) :], ""))
        elif out and ("：" in line):
            out[-1] = Turn(out[-1].said, line.split("：", 1)[1], out[-1].turn)
    return out


def clip(text: str, cap: int) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[:cap] + CLIPPED


def _size(t: Turn) -> int:
    return min(len(t.said), SAY_MAX) + min(len(t.answer), ANSWER_MAX)


def _pick(turns: Sequence[Turn], full: Mapping[str, str], short: Mapping[str, str],
          left: int) -> tuple[list[tuple[Turn, str, str]], int]:
    """从最近一轮往前排原文，放不下的那一轮查到的用要点，再放不下就停（最近一轮一定带）。"""
    chosen: list[tuple[Turn, str, str]] = []  # 最近的在前：(这一轮, 带的查到和做了的, 原文 / 要点 / 不带)
    for t in reversed(list(turns)[-TURNS_MAX:]):
        nf = full.get(t.turn, "") if t.turn else ""
        ns = short.get(t.turn, "") or nf
        if _size(t) + len(nf) <= left:
            chosen.append((t, nf, "full" if nf else ""))
        elif _size(t) + len(ns) <= left:
            chosen.append((t, ns, "short"))
        elif not chosen:  # 最近一轮的原文一定带
            chosen.append((t, "", "dropped" if nf else ""))
        else:
            break
        left -= _size(chosen[-1][0]) + len(chosen[-1][1])
    return chosen, left


def fit(
    turns: Sequence[Turn],
    *,
    full: Mapping[str, str] | None = None,
    short: Mapping[str, str] | None = None,
    summary: str = "",
    options: Sequence[str] = (),
    budget: int = CONV_BUDGET,
) -> tuple[Conversation, dict[str, Any]]:
    """本会话内容按篇幅取舍（§260）：先原文——从最近一轮往前，原话、回答、那一轮查到和做了的都用原文，
    篇幅内能放几轮放几轮；放不下的那一轮查到的用要点；再放不下，更早的几轮用会话摘要接上。
    摘要还超就按重要性去掉：先去最早几轮查到的要点，再从最早的内容删摘要。他这一句（不在这里）、
    最近两天的推荐、最近一轮的原文不去。返回要交给模型的内容和取舍记录（只记个数）。"""
    full, short = full or {}, short or {}
    options = tuple(options)[-OPTIONS_MAX:]
    room = budget - sum(len(x) for x in options)
    chosen, left = _pick(turns, full, short, room)
    if len(chosen) < len(turns) and summary:  # 有更早的要用摘要接上：先给摘要留出位置再排原文
        chosen, left = _pick(turns, full, short, room - len(summary))
        left += len(summary)
    earlier = len(turns) - len(chosen)
    summary = summary if earlier else ""
    trimmed = False
    for i in range(len(chosen) - 1, 0, -1):  # 摘要放不下：先去最早几轮查到的（最近一轮不动）
        if len(summary) <= left:
            break
        t, note, mode = chosen[i]
        if note:
            chosen[i] = (t, "", "dropped")
            left += len(note)
    if len(summary) > left:  # 还放不下：从最早的内容删摘要
        sents = [x for x in re.split(r"(?<=[。；！？])", summary) if x]
        while sents and len("".join(sents)) > max(0, left):
            sents.pop(0)
        summary, trimmed = "".join(sents), True
    kept = list(reversed(chosen))
    conv = Conversation(tuple(t for t, _, _ in kept), {t.turn: n for t, n, _ in kept if n and t.turn},
                        summary, earlier, options, frozenset(t.turn for t, _, m in kept if m == "short"))  # fmt: skip
    log = {"turns": len(kept), "earlier": earlier, "short_notes": sum(m == "short" for _, _, m in kept),
           "dropped_notes": sum(m == "dropped" for _, _, m in kept), "summary": bool(summary),
           "summary_trimmed": trimmed}  # fmt: skip
    return conv, log


@dataclass(frozen=True)
class Conversation:
    """交给作答模型的本会话上下文：最近几轮原文、各轮查到和做了的、更早的摘要。"""

    turns: tuple[Turn, ...] = ()
    notes: Mapping[str, str] | None = None  # 轮次编号 → 那一轮查到和做了的
    summary: str = ""  # 本会话摘要（更早的部分没带原文时才用）
    earlier: int = 0  # 更早没带原文的有几轮
    options: tuple[str, ...] = ()  # 最近两天推荐过的（§259），每项一行，带编号
    short: frozenset[str] = frozenset()  # 查到和做了的用的是要点的那几轮（§260）


def conversation_text(conv: Conversation, register: Callable[[str], str] | None = None) -> str:
    """拼成交给模型的一段。各轮查到和做了的登记成计算结果（register 返回编号），回答里能照样引用。"""
    if not conv.turns and not (conv.earlier and conv.summary) and not conv.options:
        return ""
    parts = ["这次对话前面的部分（用来理解这次问题里的指代、接上前面说定的事；和这次问题无关的旧话题不要重提）："]
    if conv.earlier and conv.summary:
        parts.append(f"更早的 {conv.earlier} 轮（摘要）：{conv.summary}")
    if conv.turns:
        parts.append("最近几轮原文（按先后；\"他\"是用户，\"你\"是你之前的回答）：")
    n = len(conv.turns)
    for i, t in enumerate(conv.turns):
        head = "上一轮" if i == n - 1 else f"前 {n - i} 轮"
        block = [f"【{head}】他：{clip(t.said, SAY_MAX)}"]
        if t.answer:
            block.append(f"你：{clip(t.answer, ANSWER_MAX)}")
        note = (conv.notes or {}).get(t.turn, "") if t.turn else ""
        if note:
            cid = register(f"那一轮查到、做了的：{note}") if register else ""
            what = "的要点（原文太长没带）" if t.turn in conv.short else ""
            block.append(f"你那一轮查到、做了的{what}（系统记录，他看不到；用到里面的数照样标出处{f'〔{cid}〕' if cid else ''}）：\n{note}")
        parts.append("\n".join(block))
    if conv.options:  # §259：他说"都吃了""第几个"按编号对上；数登记成计算结果，能照样引用
        lines = []
        for x in conv.options:
            cid = register(f"推荐过的：{x}") if register else ""
            lines.append(f"- {x}" + (f"〔{cid}〕" if cid else ""))
        parts.append("最近两天推荐过的（不分哪个对话；他说都吃了、第几个、就这个时按编号对上，记一餐写编号；"
                     "对不上或几种都说得通时问他一句，不要自己凑）：\n" + "\n".join(lines))
    return "\n".join(parts)


async def thread(session_id: str, current: str) -> list[Turn]:
    """本会话前面的每一轮（一问一答、原文）；读不到时返回空列表。"""
    owner, session = split_thread(session_id)
    if not owner or not session:
        return []
    try:
        from mirobody.agent.chat.message import get_chat_history

        rows: list[dict[str, Any]] = await get_chat_history(owner, session)
    except Exception as e:  # 读不到历史不影响作答
        _log.warning("history unavailable: error_type=%s", type(e).__name__)
        return []
    return turns_of(rows, current)


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


SUMMARY_EACH = 6000  # 写会话摘要时每句的防异常上限（§259 按原文写；§267 贴的报告后半也要进摘要）


async def whole(session_id: str) -> list[str]:
    """本会话全部对话（会话摘要用，按原文、每句最多 SUMMARY_EACH 字）；读不到时为空。"""
    owner, session = split_thread(session_id)
    if not owner or not session:
        return []
    try:
        from mirobody.agent.chat.message import get_chat_history

        rows: list[dict[str, Any]] = await get_chat_history(owner, session)
    except Exception as e:
        _log.warning("history unavailable: error_type=%s", type(e).__name__)
        return []
    return transcript(rows, each=SUMMARY_EACH)


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
        "description": "查这次聊天里前面说过的原文。上面带了最近几轮的原文和更早部分的摘要；"
        "他提到的内容在摘要里看不全、或原文太长被截掉了（末尾写着没带）时用。只查这一次聊天，更早的事用 recall_memory。",
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
