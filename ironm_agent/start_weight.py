"""计划里的重量（spec [2026-10-23] §229、§249）：有记录的带出来，没记录的按基本信息写好建议重量。

方案库和算出来的计划只有组数、次数，重量一栏是空的。这里不往计划里写字，只看他的训练记录：
- 记过重量的：就是"下次目标"的那个重量（管家定的或照上次，progression.targets），不另算一套；
- 最近 6 周没记录、180 天内记过重量的：写"上次 X 公斤"，提示先轻一点试；
- 180 天内没记过重量的：按性别、体重、年龄、训练水平查力量标准表，写"建议 X 公斤"（§249，start_weight.yaml）；
  器械类、技术动作、表里没有的、会用到伤处的写"从最轻的"；不知道性别或体重、不满 18 岁的照旧标"试重量"〔K28〕。
  记下重量后自然变成第一种。
不调用模型。要不要重量只看动作库：主要器械是哑铃、杠铃、壶铃、器械，按次数做的。
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Literal

from ironm_agent.exercises import Exercise, library
from ironm_agent.progression import Target, _same, rep_range
from ironm_agent.training import Plan, WorkoutLog
from ironm_agent.yamlfile import load_yaml

LOOKBACK_DAYS = 180  # 多久以前的重量还拿来参考
SHOWN = 8  # 给管家最多列几个
LOADED = frozenset({"哑铃", "杠铃", "壶铃", "器械"})
SKIP_PARTS = frozenset({"有氧", "拉伸放松"})
TRIAL_HINT = "从轻的试，做完还能再做 2–3 次就对了，记下重量"  # 〔K28〕
AGAIN_HINT = "隔了一阵，先比上次轻一点试"
SUGGEST_HINT = "按体重和水平估的，太轻太重就改，记下实际用的"  # §249
LIGHT_HINT = "从最轻的试，还能再做 2–3 次就对，记下重量"  # 〔K28〕
HURT_HINT = "会用到你记着有伤的地方，从最轻的试，疼就停"
BAR_HINT = "先用空杆练熟动作，偏重就先换哑铃"
TABLE_PATH = Path(__file__).with_name("start_weight.yaml")
LEVEL_COLUMN = {"none": 0, "lt6m": 0, "6m-2y": 1, "2y+": 2}  # 往低一档取（§249）
BAR_KG = 20.0
KETTLEBELLS = (4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32)
CAREFUL = frozenset({"pregnancy", "cardiac", "hypertension", "bp_high", "bp_crisis"})

type Kind = Literal["known", "again", "suggest", "light", "trial"]


@dataclass(frozen=True)
class Start:
    kind: Kind
    weight_kg: float | None
    text: str  # 放在动作旁的几个字："60 公斤"、"上次 60 公斤"、"试重量"
    hint: str = ""  # 怎么做的一句话；有重量的不用

    def json(self) -> dict[str, Any]:
        return {"kind": self.kind, "weight_kg": self.weight_kg, "text": self.text, "hint": self.hint}  # fmt: skip


@dataclass(frozen=True)
class Basis:
    """算建议重量用的基本信息（§249）。gear 是 None 表示不知道他有什么器材。"""

    sex: str | None = None  # male / female
    weight_kg: float | None = None
    age: int | None = None
    level: str | None = None  # none / lt6m / 6m-2y / 2y+；不知道按零基础
    gear: frozenset[str] | None = None
    injured: frozenset[str] = frozenset()  # 记着有伤的关节编码
    careful: bool = False  # 孕期、心脏病、高血压、最近血压偏高

    def usable(self) -> bool:
        return self.sex in ("male", "female") and bool(self.weight_kg) and not (self.age is not None and self.age < 18)


@cache
def table() -> dict[str, Any]:
    got: dict[str, Any] = load_yaml(TABLE_PATH)  # type: ignore[assignment]
    return got


def one_rep(lift: str, sex: str, weight_kg: float, level: str | None) -> float:
    """标准表里这个体重（两行之间取中间值、超出两头按两头）这一档能举多重〔K126〕。"""
    rows = table()["standards"][lift][sex]
    col = 1 + LEVEL_COLUMN.get(level or "none", 0)
    xs = [r[0] for r in rows]
    if weight_kg <= xs[0]:
        return float(rows[0][col])
    if weight_kg >= xs[-1]:
        return float(rows[-1][col])
    i = bisect.bisect_right(xs, weight_kg)
    lo, hi = rows[i - 1], rows[i]
    return float(lo[col] + (hi[col] - lo[col]) * (weight_kg - lo[0]) / (hi[0] - lo[0]))


def age_factor(age: int | None) -> float:
    f = 1.0
    for row in table()["age"]:
        if age is not None and age >= row["from"]:
            f = row["factor"]
    return f


def _floor(kg: float, implement: str) -> float | None:
    """往下取到器材的档位；比最轻的还轻返回 None。"""
    if implement == "杠铃":
        v = math.floor(kg / 2.5 + 1e-9) * 2.5
        return v if v >= BAR_KG else None
    if implement == "壶铃":
        got = [k for k in KETTLEBELLS if k <= kg + 1e-9]
        return float(got[-1]) if got else None
    v = math.floor(kg / 2 + 1e-9) * 2 if kg < 10 else math.floor(kg / 2.5 + 1e-9) * 2.5
    return float(v) if v >= 2 else None


def suggest(exercise: str, basis: Basis | None) -> Start | None:
    """没记录的动作的建议起步重量（§249）；None 表示信息不够，照旧"试重量"。"""
    if basis is None or not basis.usable():
        return None
    move = _by_name().get(exercise)
    if move is None:
        return None
    from ironm_agent.protools.movement import find, stressed

    m = find(exercise)
    if m is not None and basis.injured and stressed(m, basis.injured):
        return Start("light", None, "从最轻的", HURT_HINT)
    opts: dict[str, list[Any]] = table()["moves"].get(exercise) or {}
    implement = next((e for e in move.equipment if e in opts and (basis.gear is None or e in basis.gear)), None)
    if implement is None:  # 器械类、技术动作、表里没有的、他没有那种器材
        return Start("light", None, "从最轻的", LIGHT_HINT)
    lift, ratio = opts[implement]
    t = table()
    kg = (one_rep(lift, str(basis.sex), float(basis.weight_kg or 0), basis.level) * age_factor(basis.age)
          * t["working"] * ratio * (t["careful"] if basis.careful else 1.0))  # fmt: skip
    got = _floor(kg, implement)
    if got is None:
        return Start("light", None, "从空杆试", BAR_HINT) if implement == "杠铃" else Start("light", None, "从最轻的", LIGHT_HINT)
    each = "每只 " if implement in ("哑铃", "壶铃") and exercise not in t.get("one", ()) else ""
    return Start("suggest", got, f"建议 {each}{got:g} 公斤", SUGGEST_HINT)


def basis_of(profile: Any, weight_kg: float | None, items: Sequence[Any], risks: Collection[str], today: date) -> Basis:
    """从档案、最近的体重、记忆、健康提醒拼出基本信息。"""
    from ironm_agent import gear as gear_mod
    from ironm_agent.facts import injured
    from ironm_agent.latest import current

    live = [x for x in current(items) if x.status == "active"]
    level = next((str((x.data or {}).get("level") or "") for x in reversed(live) if x.category == "level"
                  and (x.data or {}).get("level") in LEVEL_COLUMN), None)  # fmt: skip
    g = gear_mod.read(live)
    have: frozenset[str] | None = None
    if g["known"]:
        have = frozenset({"哑铃", "壶铃", "杠铃", "器械"}) if g["place"] in ("gym", "both") else frozenset(g["gear"])
    return Basis(sex=getattr(profile, "sex", None), weight_kg=weight_kg, age=profile.age(today) if profile else None,
                 level=level, gear=have, injured=frozenset(injured(live)), careful=bool(CAREFUL & set(risks)))  # fmt: skip


def basis_from(snapshot: Any, items: Sequence[Any]) -> Basis:
    """这一刻的档案和健康数据（Snapshot）加上记忆。体重取最近一次记的。"""
    from ironm_agent.casefile import risks

    w = snapshot.readings.weight
    weight = max(w, key=lambda p: p[0])[1] if w else None
    keys = {x.key for x in risks(snapshot.readings, items, snapshot.today)}
    return basis_of(snapshot.profile, weight, items, keys, snapshot.today)


@cache
def _by_name() -> dict[str, Exercise]:
    return {e.name: e for e in library().exercises}


def needs_weight(exercise: str, reps: str | None = None) -> bool:
    """这个动作要不要记重量。reps 是计划里写的次数（按秒、分钟、距离写的不算）；没有计划时只看动作。"""
    if reps is not None and rep_range(reps) is None:
        return False
    move = _by_name().get(exercise)
    if move is None:  # 动作库里没有：不猜
        return False
    return not move.timed and move.part not in SKIP_PARTS and move.equipment[0] in LOADED


def last_weight(
    exercise: str, logs: Sequence[WorkoutLog], today: date
) -> tuple[date, float] | None:
    """180 天内最近一次记了重量的那天，和那天最重的一组。"""
    for log in sorted((x for x in logs if x.done), key=lambda x: x.day, reverse=True):
        if not 0 <= (today - log.day).days <= LOOKBACK_DAYS:
            continue
        weights = [s.weight_kg for it in log.items if _same(it.exercise, exercise)
                   for s in it.performed() if s.weight_kg]  # fmt: skip
        if weights:
            return log.day, max(weights)
    return None


def start_of(
    exercise: str, logs: Sequence[WorkoutLog], today: date, target: Target | None = None,
    basis: Basis | None = None,
) -> Start:
    if target is not None and target.weight_kg:
        return Start("known", target.weight_kg, f"{target.weight_kg:g} 公斤")
    seen = last_weight(exercise, logs, today)
    if seen is not None:
        return Start("again", seen[1], f"上次 {seen[1]:g} 公斤", AGAIN_HINT)
    return suggest(exercise, basis) or Start("trial", None, "试重量", TRIAL_HINT)


def starts(
    plan: Plan | None, logs: Sequence[WorkoutLog], found: Mapping[str, Target], today: date,
    basis: Basis | None = None,
) -> dict[str, Start]:
    """计划里每个要用重量的动作的重量（同名只算一次）。found 是下次目标（progression.targets）。"""
    out: dict[str, Start] = {}
    for s in plan.sessions if plan else ():
        for item in s.items:
            if item.exercise not in out and needs_weight(item.exercise, item.reps):
                out[item.exercise] = start_of(item.exercise, logs, today, found.get(item.exercise), basis)
    return out


def trial_lines(
    plan: Plan | None, logs: Sequence[WorkoutLog], today: date, basis: Basis | None = None
) -> list[str]:
    """给管家看的一行：计划里哪些动作还没有重量记录（今天要练的排前面，最多 8 个），能算的带上建议重量（§249）。"""
    if plan is None:
        return []
    first = plan.session_on(today)
    order = [x.exercise for x in first.items] if first else []
    order += [x.exercise for s in plan.sessions for x in s.items]
    names = [
        n for n in dict.fromkeys(order)
        if any(needs_weight(x.exercise, x.reps) for s in plan.sessions for x in s.items if x.exercise == n)
        and last_weight(n, logs, today) is None
    ]  # fmt: skip
    if not names:
        return []
    more = f"等 {len(names)} 个" if len(names) > SHOWN else ""
    shown = [(n, suggest(n, basis)) for n in names[:SHOWN]]
    if any(x is not None for _, x in shown):
        return [
            f"还没有重量记录的动作和建议起步重量：{'、'.join(f'{n} {x.text}' if x else f'{n} 试重量' for n, x in shown)}{more}"
            "（系统按他的性别、体重、年龄、训练水平查力量标准表估的，偏轻；照这个说，不另编公斤数；"
            "告诉他这是估的，第一次照着做，太轻太重就改，记下实际用的）"
        ]
    return [
        f"还没有重量记录的动作：{'、'.join(names[:SHOWN])}{more}"
        "（他第一次做时提醒：从轻的试起，做完还能再做 2–3 次就是对的重量，记下来下次才有目标；不替他编公斤数）"
    ]
