"""推荐过的选项（spec [2026-10-23] §259）：IronM 给出几种可以选的吃法、几个替换动作时，每个选项存成一条，
带编号和具体内容；最近两天还挂着的每轮列给作答的模型，他说"都吃了""第 1 和第 3"按编号对上，记一餐按存下的内容记。

吃的按菜单和食物库算热量和蛋白质（不用模型写的数）；吃的留到第二天结束，换动作留到一周；选了哪个也记下。
只本人可见；和 mirobody 对话内容同一种加密；注销账号时随 ironm_ 表一起删。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from ironm_agent.ddl import run_ddl

_log = logging.getLogger("ironm_agent.options")

MEAL_DAYS = 1  # 吃的推荐留到第二天结束
SWAP_DAYS = 7  # 换动作留一周
SHOW_DAYS = 2  # 列给模型最近两天的
MAX_OPTIONS = 6  # 一次最多存几项


@dataclass(frozen=True)
class Food:
    name: str
    portions: float
    food_id: str = ""  # 菜单、食物库里对上的；对不上为空
    grams: float = 0
    kcal: float | None = None
    protein: float | None = None


@dataclass(frozen=True)
class Option:
    id: str  # P12
    kind: str  # meal / swap
    label: str
    created: datetime
    expires: date
    session: str = ""
    foods: tuple[Food, ...] = ()
    exercise: str = ""  # 换动作：换成的
    instead_of: str = ""  # 换动作：被换掉的
    chosen: datetime | None = None

    @property
    def kcal(self) -> float | None:
        vals = [f.kcal for f in self.foods]
        return None if not vals or any(v is None for v in vals) else sum(v for v in vals if v is not None)

    @property
    def protein(self) -> float | None:
        vals = [f.protein for f in self.foods]
        return None if not vals or any(v is None for v in vals) else sum(v for v in vals if v is not None)

    def line(self, today: date) -> str:
        """列给模型的一行：编号、哪天推荐的、内容、热量、选没选。"""
        when = "今天" if self.created.date() == today else f"{self.created.month}月{self.created.day}日"
        if self.kind == "swap":
            what = f"换动作：{self.exercise}（替{self.instead_of}）" if self.instead_of else f"换动作：{self.exercise}"
        else:
            items = "、".join(f"{f.name}{'' if f.portions == 1 else f' {f.portions:g} 份'}" for f in self.foods)
            nums = (f"，约 {round(self.kcal)} 千卡、蛋白质 {round(self.protein or 0)} 克" if self.kcal is not None
                    else "，有的没对上菜单和食物库，热量要记的时候再算")  # fmt: skip
            what = f"吃的「{self.label}」：{items}{nums}"
        return f"{self.id}（{when}推荐）{what}；" + ("已选" if self.chosen else "没选")

    def json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "session": self.session,
                "created": self.created.isoformat(), "expires": self.expires.isoformat(),
                "foods": [f.__dict__ for f in self.foods], "exercise": self.exercise,
                "instead_of": self.instead_of, "chosen": self.chosen.isoformat() if self.chosen else None}  # fmt: skip

    @staticmethod
    def of(d: Mapping[str, Any]) -> Option:
        return Option(str(d["id"]), str(d["kind"]), str(d.get("label") or ""),
                      datetime.fromisoformat(str(d["created"])), date.fromisoformat(str(d["expires"])),
                      str(d.get("session") or ""), tuple(Food(**f) for f in d.get("foods") or []),
                      str(d.get("exercise") or ""), str(d.get("instead_of") or ""),
                      datetime.fromisoformat(str(d["chosen"])) if d.get("chosen") else None)  # fmt: skip


def resolve(raw: Any, said: str, now: datetime, session: str = "") -> list[Option]:
    """模型在回答里写的选项 → 存的选项（编号先空着）。吃的每样按连锁店菜单、食物库对上，热量蛋白质由这里算。
    写得不对的项跳过。"""
    from ironm_agent.chains import branded
    from ironm_agent.foods import find
    from ironm_agent.meals import food_line

    out: list[Option] = []
    if not isinstance(raw, list):
        return out
    for x in raw[:MAX_OPTIONS]:
        if not isinstance(x, Mapping):
            continue
        if x.get("exercise"):
            out.append(Option("", "swap", str(x["exercise"])[:30], now, now.date() + timedelta(days=SWAP_DAYS),
                              session, exercise=str(x["exercise"])[:30],
                              instead_of=str(x.get("instead_of") or "")[:30]))  # fmt: skip
            continue
        foods = []
        for f in x.get("foods") or []:
            if not isinstance(f, Mapping) or not str(f.get("name") or "").strip():
                continue
            name = str(f["name"]).strip()[:30]
            n = f.get("portions")
            portions = float(n) if isinstance(n, int | float) and 0 < float(n) <= 10 else 1.0
            known = branded(name, said) or find(name)
            if known is None:
                foods.append(Food(name, portions))
                continue
            line = food_line(known.name, portions * known.unit_g, food_id=known.id)
            foods.append(Food(known.name, portions, known.id, line.grams, line.macros.kcal, line.macros.protein_g))
        if foods:
            label = str(x.get("name") or "").strip()[:30] or "、".join(f.name for f in foods)[:30]
            out.append(Option("", "meal", label, now, now.date() + timedelta(days=MEAL_DAYS), session,
                              tuple(foods)))  # fmt: skip
    return out


def numbered(new: Sequence[Option], existing: Sequence[Option]) -> list[Option]:
    """接着这个人已有的编号往下排：P1、P2……"""
    used = [int(x.id[1:]) for x in existing if x.id[1:].isdigit()]
    start = max(used, default=0) + 1
    return [replace(x, id=f"P{start + i}") for i, x in enumerate(new)]


class OptionStore(Protocol):
    async def recent(self, user_id: str, today: date) -> list[Option]: ...
    async def add(self, user_id: str, options: Sequence[Option]) -> list[Option]: ...
    async def choose(self, user_id: str, ids: Sequence[str]) -> None: ...


@dataclass
class OptionBook:
    """内存版（测试用）。"""

    rows: dict[str, list[Option]] = field(default_factory=dict)

    async def recent(self, user_id: str, today: date) -> list[Option]:
        return [x for x in self.rows.get(user_id, []) if x.expires >= today
                and (today - x.created.date()).days < SHOW_DAYS]  # fmt: skip

    async def add(self, user_id: str, options: Sequence[Option]) -> list[Option]:
        got = numbered(options, self.rows.get(user_id, []))
        self.rows.setdefault(user_id, []).extend(got)
        return got

    async def choose(self, user_id: str, ids: Sequence[str]) -> None:
        now = datetime.now().astimezone()
        self.rows[user_id] = [replace(x, chosen=now) if x.id in ids else x for x in self.rows.get(user_id, [])]


DDL = """
CREATE TABLE IF NOT EXISTS ironm_option (
    user_id    text NOT NULL,
    id         text NOT NULL,
    content    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_on date NOT NULL,
    PRIMARY KEY (user_id, id)
)
"""


class PgOptions:
    """数据库版：和 mirobody 对话内容同一种加密。"""

    def __init__(self) -> None:
        self._ready = False

    async def _q(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from mirobody.utils.db import execute_query

        if not self._ready:
            await run_ddl(DDL)
            self._ready = True
        rows = await execute_query(sql, params, log_sql=False)
        return rows if isinstance(rows, list) else []

    async def _all(self, user_id: str, since: date) -> list[Option]:
        got = await self._q(
            "SELECT decrypt_content(content) AS content FROM ironm_option"
            " WHERE user_id = :uid AND created_at >= :since ORDER BY created_at",
            {"uid": user_id, "since": since},
        )
        return [Option.of(json.loads(r["content"])) for r in got if r.get("content")]

    async def recent(self, user_id: str, today: date) -> list[Option]:
        return [x for x in await self._all(user_id, today - timedelta(days=SHOW_DAYS - 1)) if x.expires >= today]

    async def add(self, user_id: str, options: Sequence[Option]) -> list[Option]:
        last = await self._q("SELECT id FROM ironm_option WHERE user_id = :uid", {"uid": user_id})
        got = numbered(options, [Option(str(r["id"]), "", "", datetime.now(), date.today()) for r in last])
        for x in got:
            await self._q(
                "INSERT INTO ironm_option (user_id, id, content, expires_on) VALUES (:uid, :id, encrypt_content(:c), :e)",
                {"uid": user_id, "id": x.id, "c": json.dumps(x.json(), ensure_ascii=False), "e": x.expires},
            )
        return got

    async def choose(self, user_id: str, ids: Sequence[str]) -> None:
        if not ids:
            return
        got = await self._q(
            "SELECT id, decrypt_content(content) AS content FROM ironm_option WHERE user_id = :uid AND id = ANY(:ids)",
            {"uid": user_id, "ids": list(ids)},
        )
        now = datetime.now().astimezone()
        for r in got:
            x = replace(Option.of(json.loads(r["content"])), chosen=now)
            await self._q(
                "UPDATE ironm_option SET content = encrypt_content(:c) WHERE user_id = :uid AND id = :id RETURNING 1",
                {"uid": user_id, "id": x.id, "c": json.dumps(x.json(), ensure_ascii=False)},
            )


_default: OptionStore | None = None


def default_options() -> OptionStore:
    global _default
    if _default is None:
        _default = PgOptions()
    return _default


def option_lines(options: Sequence[Option]) -> tuple[list[Any], list[str]]:
    """推荐 → 一餐里的食物行（按推荐时存下的食物和克数算，和推荐时一致）；对不上菜单和食物库的列出来。"""
    from ironm_agent.meals import food_line

    lines, missed = [], []
    for o in options:
        for f in o.foods:
            if f.food_id and f.grams:
                lines.append(food_line(f.name, f.grams, food_id=f.food_id))
            else:
                missed.append(f.name)
    return lines, missed


def brief(o: Option) -> dict[str, Any]:
    """给页面的一项（推荐下面的按钮用，§261）。"""
    return {"id": o.id, "kind": o.kind, "label": o.label, "kcal": round(o.kcal) if o.kcal is not None else None,
            "exercise": o.exercise, "instead_of": o.instead_of, "chosen": bool(o.chosen)}  # fmt: skip
