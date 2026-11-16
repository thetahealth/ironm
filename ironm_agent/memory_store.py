"""Agent 记忆（L1 记忆条目）的存储：一条条带类别、状态、原话片段的记忆。

- 情况变化时不改原来那条，而是新增一条并把旧的标为"被新说法取代"，旧的不再用于回答；
- 内容与原话片段加密存储（与 mirobody 对话内容同一种加密），日志里不写记忆内容；
- 测试和考题用内存版（MemoryBook），线上用数据库版（PgMemory），两者接口一样。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from ironm_agent.ddl import run_ddl

_log = logging.getLogger("ironm_agent.memory_store")

type Category = Literal[
    "injury",
    "equipment",
    "schedule",
    "diet",
    "preference",
    "goal",
    "style",
    "event",
    "advice",
    "followup",
    "level",
    "special",
]
type Status = Literal["active", "pending", "resolved", "superseded"]
type Origin = Literal["said", "inferred", "advice", "edited", "data", "system"]

CATEGORIES: dict[Category, str] = {
    "injury": "伤病与身体状况",
    "equipment": "器械与场地",
    "schedule": "可练时间与作息",
    "diet": "饮食限制与习惯",
    "preference": "喜好与讨厌",
    "goal": "目标",
    "style": "回答风格",
    "event": "近期事件",
    "advice": "给过的关键建议",
    "followup": "待跟进",
    "level": "训练水平",
    "special": "特殊情况",  # 孕期、心脏病、糖尿病、高血压、近期手术（§148）
}
STATUSES: tuple[Status, ...] = ("active", "pending", "resolved", "superseded")
ORIGINS: dict[Origin, str] = {
    "said": "你说的",
    "inferred": "系统推断",
    "advice": "给过的建议",
    "edited": "你改过的",
    "data": "训练记录",
    "system": "系统",
}


# 只说一天的相对说法（§103）：记忆里写成日期，过几天再读不会当成那天
_RELATIVE = re.compile(
    r"今天早上|今天晚上|今天|今日|今早|今晨|今晚|昨天晚上|昨天|昨日|昨晚|前天|明天|明早|明晚|后天"
)
_SHIFT = {"今": 0, "昨": -1, "前": -2, "明": 1, "后": 2}


def absolute(text: str, day: date) -> str:
    """把"今天、昨晚、明天……"换成具体日期（day 是说这句话的那天）。"""

    def one(m: re.Match[str]) -> str:
        w = m.group(0)
        d = day + timedelta(days=_SHIFT[w[0]])
        part = (
            "早上"
            if w[-1] in "早晨" or w.endswith("早上")
            else "晚上"
            if w[-1] in "晚" or w.endswith("晚上")
            else ""
        )
        return f"{d.month}月{d.day}日{part}"

    return _RELATIVE.sub(one, text)


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class Item:
    id: str
    category: Category
    content: str  # 一句简短的说法
    evidence: str  # 支撑这条记忆的原话片段（给过的建议则是回答里的片段）
    origin: Origin
    status: Status
    created: datetime
    updated: datetime
    supersedes: str | None = None  # 取代了哪一条
    until: date | None = None  # 近况到哪天为止（含这天；spec [2026-10-23] §68），没写的按 14 天
    data: dict[str, Any] = field(
        default_factory=dict
    )  # 伤病、特殊情况的结构化字段（§148，facts.py）
    key: str = ""  # 同一件事的键：同键只有一条生效（§148）


class StaleMemory(Exception):
    """要改的那条记忆已经被别处改掉了（被取代或删了）：这一轮的改动整体不写（§207）。"""


HELD_MAX = 5  # 没记下的话最多留最近几句（§204）
HELD_DAYS = 7  # 留几天，过了不再补记


@dataclass(frozen=True)
class Held:
    """回答后整理记忆没成功、先留着的一句话（§204）：下一次整理时一起交给模型。"""

    id: str
    text: str  # 开头写明哪天说的，句子里的"今天、昨晚"已经换成日期
    at: datetime


def _waiting(rows: Sequence[Held], at: datetime | None) -> list[Held]:
    """还在留着的：7 天内、最近 5 句，早说的在前。"""
    cut = (at or now()) - timedelta(days=HELD_DAYS)
    return sorted((h for h in rows if h.at >= cut), key=lambda h: h.at)[-HELD_MAX:]


@dataclass(frozen=True)
class Change:
    """提取结果落到存储上的一步：新增一条，或改一条的状态。"""

    kind: Literal["insert", "status"]
    item: Item | None = None
    id: str = ""
    status: Status = "active"


class MemoryStore(Protocol):
    async def items(
        self, user_id: str, *, current_only: bool = True, recent: date | None = None
    ) -> list[Item]: ...  # recent：给今天的日期时不取归档了的历史（§157）
    async def apply(self, user_id: str, changes: Sequence[Change]) -> None: ...
    async def edit(self, user_id: str, item_id: str, content: str) -> Item | None: ...
    async def confirm(self, user_id: str, item_id: str) -> Item | None: ...
    async def delete(self, user_id: str, item_id: str) -> bool: ...
    async def set_fields(
        self, user_id: str, item_id: str, category: Category, data: dict[str, Any], key: str
    ) -> bool: ...
    async def paused(self, user_id: str) -> bool: ...
    async def set_paused(self, user_id: str, paused: bool) -> None: ...
    async def hold(self, user_id: str, text: str, at: datetime | None = None) -> None: ...
    async def held(self, user_id: str, at: datetime | None = None) -> list[Held]: ...
    async def release(self, user_id: str, ids: Sequence[str]) -> None: ...


def chain(items: Sequence[Item], item_id: str) -> set[str]:
    """一条记忆和它取代过的所有旧版本（删除时一起删，不留下同样的内容）。"""
    by_id = {x.id: x for x in items}
    out: set[str] = set()
    cur = by_id.get(item_id)
    while cur is not None and cur.id not in out:
        out.add(cur.id)
        cur = by_id.get(cur.supersedes or "")
    return out


class MemoryBook:
    """内存版：测试与考题用。"""

    def __init__(self) -> None:
        self._items: dict[str, list[Item]] = {}
        self._paused: set[str] = set()
        self._held: dict[str, list[Held]] = {}

    async def items(
        self, user_id: str, *, current_only: bool = True, recent: date | None = None
    ) -> list[Item]:
        from ironm_agent.memory_view import archived

        rows = self._items.get(user_id, [])
        for i, x in enumerate(rows):  # 读到还没补字段的伤病、特殊情况：当场补上（§148 迁移）
            if (got := _filled(x)) is not None:
                rows[i] = got
        rows = [x for x in rows if recent is None or not archived(x, recent)]
        return [x for x in rows if x.status != "superseded"] if current_only else rows

    async def apply(self, user_id: str, changes: Sequence[Change]) -> None:
        rows = self._items.setdefault(user_id, [])
        live = {x.id for x in rows if x.status != "superseded"}
        if any(c.kind == "status" and c.status == "superseded" and c.id not in live for c in changes):
            raise StaleMemory  # 要取代的那条已经被别处改掉：整批不写（§207）；撤销（从被取代改回来）不算
        for c in changes:
            if c.kind == "insert" and c.item is not None:
                rows.append(c.item)
            elif c.kind == "status":
                for i, x in enumerate(rows):
                    if x.id == c.id:
                        rows[i] = replace(x, status=c.status, updated=now())

    async def edit(self, user_id: str, item_id: str, content: str) -> Item | None:
        rows = self._items.get(user_id, [])
        for i, x in enumerate(rows):
            if x.id == item_id and x.status != "superseded":
                cat, data, key = _edited(x, content) or (x.category, x.data, x.key)
                rows[i] = replace(x, content=content, origin="edited", updated=now(), category=cat,
                                  data=data, key=key,
                                  status="active" if x.status == "pending" else x.status)  # fmt: skip
                return rows[i]
        return None

    async def set_fields(
        self, user_id: str, item_id: str, category: Category, data: dict[str, Any], key: str
    ) -> bool:
        rows = self._items.get(user_id, [])
        for i, x in enumerate(rows):
            if x.id == item_id:
                rows[i] = replace(x, category=category, data=dict(data), key=key)
                return True
        return False

    async def confirm(self, user_id: str, item_id: str) -> Item | None:
        rows = self._items.get(user_id, [])
        for i, x in enumerate(rows):
            if x.id == item_id and x.status == "pending":
                rows[i] = replace(x, status="active", updated=now())
                return rows[i]
        return None

    async def delete(self, user_id: str, item_id: str) -> bool:
        rows = self._items.get(user_id, [])
        if not any(x.id == item_id and x.status != "superseded" for x in rows):
            return False
        gone = chain(rows, item_id)
        self._items[user_id] = [x for x in rows if x.id not in gone]
        return True

    async def paused(self, user_id: str) -> bool:
        return user_id in self._paused

    async def set_paused(self, user_id: str, paused: bool) -> None:
        (self._paused.add if paused else self._paused.discard)(user_id)
        if paused:  # 暂停了：留着等补记的话也不要了（§204）
            self._held.pop(user_id, None)

    async def hold(self, user_id: str, text: str, at: datetime | None = None) -> None:
        rows = self._held.setdefault(user_id, [])
        if all(h.text != text for h in rows):  # 同一句不重复留
            rows.append(Held(new_id(), text, at or now()))
        self._held[user_id] = sorted(rows, key=lambda h: h.at)[-HELD_MAX:]

    async def held(self, user_id: str, at: datetime | None = None) -> list[Held]:
        return _waiting(self._held.get(user_id, []), at)

    async def release(self, user_id: str, ids: Sequence[str]) -> None:
        self._held[user_id] = [h for h in self._held.get(user_id, []) if h.id not in set(ids)]


DDL = (
    """
CREATE TABLE IF NOT EXISTS ironm_memory (
    id         text PRIMARY KEY,
    user_id    text NOT NULL,
    category   text NOT NULL,
    content    text NOT NULL,
    evidence   text NOT NULL,
    origin     text NOT NULL,
    status     text NOT NULL,
    supersedes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
""",
    "CREATE INDEX IF NOT EXISTS ironm_memory_user ON ironm_memory (user_id, status)",
    "ALTER TABLE ironm_memory ADD COLUMN IF NOT EXISTS until date",
    "ALTER TABLE ironm_memory ADD COLUMN IF NOT EXISTS data text",  # 结构化字段，加密（§148）
    "ALTER TABLE ironm_memory ADD COLUMN IF NOT EXISTS key text",
    """
CREATE TABLE IF NOT EXISTS ironm_memory_state (
    user_id    text PRIMARY KEY,
    paused     boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
)
""",
    # 没记下、等下次补记的话（§204）；content 加密
    """
CREATE TABLE IF NOT EXISTS ironm_memory_held (
    id      text PRIMARY KEY,
    user_id text NOT NULL,
    content text NOT NULL,
    said_at timestamptz NOT NULL DEFAULT now()
)
""",
    "CREATE INDEX IF NOT EXISTS ironm_memory_held_user ON ironm_memory_held (user_id, said_at)",
)

# content、evidence 是加密后的文字；加密失败时 encrypt_content 返回 NULL，NOT NULL 约束让这一条写不进去
ARCHIVED = (
    " AND NOT ((status = 'superseded' AND updated_at < :cut)"
    " OR (status = 'resolved' AND category NOT IN ('injury', 'special') AND updated_at < :cut)"
    " OR (category IN ('event', 'followup') AND ((until IS NOT NULL AND until < :cut_day)"
    " OR (until IS NULL AND created_at < :cut_events)))"
    " OR (category = 'advice' AND created_at < :cut)"
    " OR (origin = 'data' AND updated_at < :cut))"
)
_SELECT = (
    "SELECT id, category, decrypt_content(content) AS content, decrypt_content(evidence) AS evidence,"
    " origin, status, supersedes, until, created_at, updated_at,"
    " CASE WHEN data IS NULL THEN NULL ELSE decrypt_content(data) END AS data, key FROM ironm_memory"
)


def _row(r: dict[str, Any]) -> Item:
    return Item(
        id=str(r["id"]),
        category=cast(Category, r["category"]),
        content=str(r.get("content") or ""),
        evidence=str(r.get("evidence") or ""),
        origin=cast(Origin, r["origin"]),
        status=cast(Status, r["status"]),
        created=r["created_at"],
        updated=r["updated_at"],
        supersedes=r.get("supersedes"),
        until=r.get("until"),
        data=_data(r.get("data")),
        key=str(r.get("key") or ""),
    )


def _data(raw: Any) -> dict[str, Any]:
    try:
        got = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _fields(category: Category, content: str) -> tuple[Category, dict[str, Any], str]:
    """伤病、特殊情况按内容补字段（改了内容、迁移时用；§148）。"""
    from ironm_agent.facts import derive, key_of

    cat, data = derive(category, content)
    return cast(Category, cat), data, key_of(cat, data)


def _edited(x: Item, content: str) -> tuple[Category, dict[str, Any], str] | None:
    """他在"我"页改了一条的内容：伤病、器械这类按新内容重新补字段（§148、§150）；喜好、饮食、风格的主题
    在新内容里还找得到就留着，找不到说明说的已经是别的东西，主题去掉（§207）。别的类别不动（返回 None）。"""
    from ironm_agent.facts import STRUCTURED
    from ironm_agent.memory_extract import TOPICAL, norm

    if x.category in STRUCTURED:
        return _fields(x.category, content)
    if x.category in TOPICAL and x.key:
        topic = str(x.data.get("topic") or "")
        fits = bool(topic) and norm(topic) in norm(content)
        return (x.category, dict(x.data), x.key) if fits else (x.category, {}, "")
    return None


def _filled(x: Item) -> Item | None:
    """还没补字段的伤病、特殊情况、器械、可练时间（§148、§150 之前记的）：按内容补上；补不出返回 None。
    系统从训练记录推出的作息（"通常在周一、周三练"）不带键：两条推出来的不互相顶掉（复核 11-03）。"""
    from ironm_agent.facts import STRUCTURED

    if x.category not in STRUCTURED or x.data or x.status == "superseded":
        return None
    cat, data, key = _fields(x.category, x.content)
    if x.origin == "data":
        key = ""
    return replace(x, category=cat, data=data, key=key) if data else None


def _dumps(data: dict[str, Any]) -> str | None:
    return json.dumps(data, ensure_ascii=False) if data else None


class PgMemory:
    """数据库版：与 mirobody 同一个数据库里 iron-m 自己的表，连接沿用 mirobody 的（带加密密钥）。"""

    def __init__(self) -> None:
        self._ready = False

    async def _prepare(self) -> None:
        if not self._ready:
            await run_ddl(*DDL)
            self._ready = True

    async def _q(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from mirobody.utils.db import execute_query

        await self._prepare()
        rows = await execute_query(sql, params or {}, log_sql=False)
        return rows if isinstance(rows, list) else []

    async def items(
        self, user_id: str, *, current_only: bool = True, recent: date | None = None
    ) -> list[Item]:
        where = " WHERE user_id = :uid" + (" AND status <> 'superseded'" if current_only else "")
        params: dict[str, Any] = {"uid": user_id}
        if recent is not None:  # 不取归档了的历史（§157，和 memory_view.archived 同一个口径）
            from ironm_agent.memory_view import ARCHIVE_DAYS, _midnight

            where += ARCHIVED
            params |= {"cut": _midnight(recent - timedelta(days=ARCHIVE_DAYS)),
                       "cut_day": recent - timedelta(days=ARCHIVE_DAYS),
                       "cut_events": _midnight(recent - timedelta(days=ARCHIVE_DAYS + 7))}  # fmt: skip
        rows = await self._q(_SELECT + where + " ORDER BY created_at", params)
        out = [_row(r) for r in rows]
        for i, x in enumerate(out):  # 读到还没补字段的伤病、特殊情况：当场补上写回（§148 迁移）
            if (got := _filled(x)) is not None:
                out[i] = got
                try:
                    await self.set_fields(user_id, x.id, got.category, got.data, got.key)
                except Exception as e:  # 写不回不影响这次读，下次再补
                    _log.warning("memory fields backfill failed: error_type=%s", type(e).__name__)
        return out

    async def apply(self, user_id: str, changes: Sequence[Change]) -> None:
        """一轮的改动放在一个事务里（§204）：要么都生效，要么都不生效。
        更新一条是"旧的作废 + 写入新的"两句，分开提交时新的写不进去（加密失败、连接断），旧的就白白作废了。"""
        from mirobody.utils.db import transaction

        work: list[tuple[str, dict[str, Any]]] = []
        for c in changes:
            if c.kind == "insert" and c.item is not None:
                x = c.item
                work.append((
                    "INSERT INTO ironm_memory (id, user_id, category, content, evidence, origin,"
                    " status, supersedes, until, created_at, updated_at, data, key) VALUES (:id, :uid,"
                    " :category, encrypt_content(:content), encrypt_content(:evidence), :origin, :status,"
                    " :supersedes, :until, :created, :updated,"
                    " CASE WHEN CAST(:data AS text) IS NULL THEN NULL ELSE encrypt_content(:data) END, :key)",
                    {"id": x.id, "uid": user_id, "category": x.category, "content": x.content,
                     "evidence": x.evidence, "origin": x.origin, "status": x.status,
                     "supersedes": x.supersedes, "until": x.until, "created": x.created, "updated": x.updated,
                     "data": _dumps(x.data), "key": x.key or None},
                ))  # fmt: skip
            elif c.kind == "status":
                # 取代一条时它得还没被取代（§207）；撤销（从被取代改回来，改目标时用）不受这个限制
                guard = " AND status <> 'superseded'" if c.status == "superseded" else ""
                work.append((
                    "UPDATE ironm_memory SET status = :status, updated_at = now()"
                    f" WHERE id = :id AND user_id = :uid{guard} RETURNING id",
                    {"status": c.status, "id": c.id, "uid": user_id},
                ))  # fmt: skip
        if not work:
            return
        await self._prepare()
        async with transaction() as tx:
            for sql, params in work:
                got = await tx.execute(sql, params)
                if sql.startswith("UPDATE") and params["status"] == "superseded" and not got:
                    raise StaleMemory  # 这条已经被别处改掉（别的进程、他自己删了）：整批退回（§207）

    async def edit(self, user_id: str, item_id: str, content: str) -> Item | None:
        rows = await self._q(
            "UPDATE ironm_memory SET content = encrypt_content(:content), origin = 'edited',"
            " status = CASE WHEN status = 'pending' THEN 'active' ELSE status END, updated_at = now()"
            " WHERE id = :id AND user_id = :uid AND status <> 'superseded' RETURNING id",
            {"content": content, "id": item_id, "uid": user_id},
        )
        if not rows:
            return None
        got = await self._one(user_id, item_id)
        new = _edited(got, content) if got is not None else None
        # 改了内容：字段跟着补（§148、§150）；主题对不上了就去掉（§207）
        if got is not None and new is not None and new != (got.category, got.data, got.key):
            await self.set_fields(user_id, item_id, *new)
            got = await self._one(user_id, item_id)
        return got

    async def confirm(self, user_id: str, item_id: str) -> Item | None:
        rows = await self._q(
            "UPDATE ironm_memory SET status = 'active', updated_at = now()"
            " WHERE id = :id AND user_id = :uid AND status = 'pending' RETURNING id",
            {"id": item_id, "uid": user_id},
        )
        return await self._one(user_id, item_id) if rows else None

    async def set_fields(
        self, user_id: str, item_id: str, category: Category, data: dict[str, Any], key: str
    ) -> bool:
        """只改类别和结构化字段（迁移、改了内容后补字段；§148），不动内容、原话和历史。"""
        rows = await self._q(
            "UPDATE ironm_memory SET category = :category,"
            " data = CASE WHEN CAST(:data AS text) IS NULL THEN NULL ELSE encrypt_content(:data) END,"
            " key = :key WHERE id = :id AND user_id = :uid RETURNING id",
            {"category": category, "data": _dumps(data), "key": key or None, "id": item_id, "uid": user_id},
        )  # fmt: skip
        return bool(rows)

    async def delete(self, user_id: str, item_id: str) -> bool:
        rows = await self.items(user_id, current_only=False)
        if not any(x.id == item_id and x.status != "superseded" for x in rows):
            return False
        await self._q(
            "DELETE FROM ironm_memory WHERE user_id = :uid AND id = ANY(:ids)",
            {"uid": user_id, "ids": sorted(chain(rows, item_id))},
        )
        return True

    async def _one(self, user_id: str, item_id: str) -> Item | None:
        rows = await self._q(_SELECT + " WHERE user_id = :uid AND id = :id",
                             {"uid": user_id, "id": item_id})  # fmt: skip
        return _row(rows[0]) if rows else None

    async def paused(self, user_id: str) -> bool:
        rows = await self._q(
            "SELECT paused FROM ironm_memory_state WHERE user_id = :uid", {"uid": user_id}
        )
        return bool(rows and rows[0].get("paused"))

    async def set_paused(self, user_id: str, paused: bool) -> None:
        await self._q(
            "INSERT INTO ironm_memory_state (user_id, paused) VALUES (:uid, :paused)"
            " ON CONFLICT (user_id) DO UPDATE SET paused = EXCLUDED.paused, updated_at = now()",
            {"uid": user_id, "paused": paused},
        )
        if paused:  # 暂停了：留着等补记的话也不要了（§204）
            await self._q("DELETE FROM ironm_memory_held WHERE user_id = :uid", {"uid": user_id})

    async def _all_held(self, user_id: str) -> list[Held]:
        rows = await self._q(
            "SELECT id, decrypt_content(content) AS content, said_at FROM ironm_memory_held"
            " WHERE user_id = :uid ORDER BY said_at",
            {"uid": user_id},
        )
        return [Held(str(r["id"]), str(r.get("content") or ""), r["said_at"]) for r in rows]

    async def hold(self, user_id: str, text: str, at: datetime | None = None) -> None:
        """留一句等下次补记：同一句不重复留；过了 7 天的、超出最近 5 句的顺手清掉。"""
        at = at or now()
        rows = await self._all_held(user_id)
        if all(h.text != text for h in rows):
            mine = Held(new_id(), text, at)
            await self._q(
                "INSERT INTO ironm_memory_held (id, user_id, content, said_at)"
                " VALUES (:id, :uid, encrypt_content(:content), :at)",
                {"id": mine.id, "uid": user_id, "content": text, "at": at},
            )
            rows.append(mine)
        keep = {h.id for h in _waiting(rows, at)}
        gone = sorted(h.id for h in rows if h.id not in keep)
        if gone:
            await self.release(user_id, gone)

    async def held(self, user_id: str, at: datetime | None = None) -> list[Held]:
        return _waiting(await self._all_held(user_id), at)

    async def release(self, user_id: str, ids: Sequence[str]) -> None:
        if ids:
            await self._q(
                "DELETE FROM ironm_memory_held WHERE user_id = :uid AND id = ANY(:ids)",
                {"uid": user_id, "ids": sorted(ids)},
            )


_default: PgMemory | None = None


def default_store() -> MemoryStore:
    """线上用的记忆存储（问答和记忆接口共用一个）。"""
    global _default
    if _default is None:
        _default = PgMemory()
    return _default
