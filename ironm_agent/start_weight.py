"""计划里的重量（spec [2026-10-23] §229）：有记录的带出来，没记录的第一次试出来记下。

方案库和算出来的计划只有组数、次数，重量一栏是空的。这里不往计划里写字，只看他的训练记录：
- 记过重量的：就是"下次目标"的那个重量（管家定的或照上次，progression.targets），不另算一套；
- 最近 6 周没记录、180 天内记过重量的：写"上次 X 公斤"，提示先轻一点试；
- 180 天内没记过重量的：标"试重量"，带一句怎么试〔K28〕。记下重量后自然变成第一种。
不编公斤数、不调用模型。要不要重量只看动作库：主要器械是哑铃、杠铃、壶铃、器械，按次数做的。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache
from typing import Any, Literal

from ironm_agent.exercises import Exercise, library
from ironm_agent.progression import Target, _same, rep_range
from ironm_agent.training import Plan, WorkoutLog

LOOKBACK_DAYS = 180  # 多久以前的重量还拿来参考
SHOWN = 8  # 给管家最多列几个
LOADED = frozenset({"哑铃", "杠铃", "壶铃", "器械"})
SKIP_PARTS = frozenset({"有氧", "拉伸放松"})
TRIAL_HINT = "从轻的试，做完还能再做 2–3 次就对了，记下重量"  # 〔K28〕
AGAIN_HINT = "隔了一阵，先比上次轻一点试"

type Kind = Literal["known", "again", "trial"]


@dataclass(frozen=True)
class Start:
    kind: Kind
    weight_kg: float | None
    text: str  # 放在动作旁的几个字："60 公斤"、"上次 60 公斤"、"试重量"
    hint: str = ""  # 怎么做的一句话；有重量的不用

    def json(self) -> dict[str, Any]:
        return {"kind": self.kind, "weight_kg": self.weight_kg, "text": self.text, "hint": self.hint}  # fmt: skip


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
    exercise: str, logs: Sequence[WorkoutLog], today: date, target: Target | None = None
) -> Start:
    if target is not None and target.weight_kg:
        return Start("known", target.weight_kg, f"{target.weight_kg:g} 公斤")
    seen = last_weight(exercise, logs, today)
    if seen is not None:
        return Start("again", seen[1], f"上次 {seen[1]:g} 公斤", AGAIN_HINT)
    return Start("trial", None, "试重量", TRIAL_HINT)


def starts(
    plan: Plan | None, logs: Sequence[WorkoutLog], found: Mapping[str, Target], today: date
) -> dict[str, Start]:
    """计划里每个要用重量的动作的重量（同名只算一次）。found 是下次目标（progression.targets）。"""
    out: dict[str, Start] = {}
    for s in plan.sessions if plan else ():
        for item in s.items:
            if item.exercise not in out and needs_weight(item.exercise, item.reps):
                out[item.exercise] = start_of(item.exercise, logs, today, found.get(item.exercise))
    return out


def trial_lines(plan: Plan | None, logs: Sequence[WorkoutLog], today: date) -> list[str]:
    """给管家看的一行：计划里哪些动作还没有重量记录（今天要练的排前面，最多 8 个）。"""
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
    return [
        f"还没有重量记录的动作：{'、'.join(names[:SHOWN])}{more}"
        "（他第一次做时提醒：从轻的试起，做完还能再做 2–3 次就是对的重量，记下来下次才有目标；不替他编公斤数）"
    ]
