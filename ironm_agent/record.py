"""记录页要的数（spec [2026-10-23] §272）：一段时间练了多少、各部位练了几组和每个动作从多重到多重、
一周七天各练了什么（肌肉图用）、一次训练的每一组。只读训练记录和计划，规则算，不调用模型。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Literal

from ironm_agent.share import best, records, set_count, volume
from ironm_agent.training import LogItem, Plan, StoredLog, WorkoutLog, week_start, weekday_name

Range = Literal["week", "month", "all"]
PARTS = ("腿臀", "背", "胸", "手臂", "肩", "核心")  # 单列一行的部位；有氧、全身、拉伸放松不单列


@lru_cache(maxsize=1)
def part_map() -> dict[str, str]:
    """动作名 → 部位，照动作库。"""
    from ironm_agent.exercises import library

    return {x.name: x.part for x in library().exercises}


def _n(it: LogItem) -> int:
    return len(it.done_sets) or it.sets or 0


def top_part(items: Sequence[Any], part_of: Mapping[str, str]) -> str:
    """这次练得最多的部位（按组数；一样多取先练的）。动作库里没有的动作不算。"""
    count: Counter[str] = Counter()
    for it in items:
        p = part_of.get(it.exercise)
        if p:
            count[p] += _n(it) if isinstance(it, LogItem) else it.sets
    return max(count, key=lambda p: count[p]) if count else ""


def span(kind: Range, today: date) -> tuple[date, date]:
    if kind == "week":
        monday = week_start(today)
        return monday, monday + timedelta(days=6)
    if kind == "month":
        first = today.replace(day=1)
        return first, (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return date.min, today


def _g(x: float) -> float | int:
    return int(x) if float(x).is_integer() else round(x, 1)


def progress(
    logs: Sequence[WorkoutLog], part_of: Mapping[str, str], until: date
) -> dict[str, list[dict[str, Any]]]:
    """每个部位练过、记了重量的动作：第一次和最近一次最好的一组、涨了多少、一路的重量。按涨得多少排。"""
    track: dict[str, list[tuple[float, int]]] = {}
    for x in sorted((x for x in logs if x.done and x.day <= until), key=lambda r: r.day):
        for it in x.items:
            b = best(it)
            if b is not None:
                track.setdefault(it.exercise, []).append(b)
    out: dict[str, list[dict[str, Any]]] = {}
    for name, sets in track.items():
        first, last = sets[0], sets[-1]
        out.setdefault(part_of.get(name, ""), []).append({
            "exercise": name, "from_kg": _g(first[0]), "to_kg": _g(last[0]), "reps": last[1],
            "gain": _g(round(last[0] - first[0], 1)), "series": [_g(w) for w, _ in sets],
        })  # fmt: skip
    for moves in out.values():
        moves.sort(key=lambda m: (-m["gain"], -m["to_kg"]))
    return out


def _done_names(logs: Sequence[WorkoutLog], monday: date) -> set[str]:
    return {x.session for x in logs if x.done and monday <= x.day <= monday + timedelta(days=6)}


def ahead(plan: Plan | None, logs: Sequence[WorkoutLog], today: date) -> list[tuple[date, Any]]:
    """这周还没到（今天起）、也还没练的那几次计划训练。"""
    if plan is None:
        return []
    monday, done = week_start(today), _done_names(logs, week_start(today))
    out = []
    for i in range(today.weekday(), 7):
        day = monday + timedelta(days=i)
        s = plan.session_on(day)
        if s is not None and s.name not in done:
            out.append((day, s))
    return out


def _when(day: date, today: date) -> str:
    return "今天" if day == today else weekday_name(day.weekday() + 1)


def coming(
    plan: Plan | None, logs: Sequence[WorkoutLog], part_of: Mapping[str, str], today: date
) -> dict[str, str]:
    """这周还没练到、但计划里后面会练到的部位 → 哪天（"周三"；今天的写"今天"）。照计划里那次训练的动作。"""
    out: dict[str, str] = {}
    for day, s in ahead(plan, logs, today):
        for it in s.items:
            p = part_of.get(it.exercise)
            if p in PARTS:
                out.setdefault(p, _when(day, today))
    return out


def streak_weeks(logs: Sequence[WorkoutLog], today: date) -> int:
    """现在连着几周每周都练了：从这周往回数；这周还没练就从上周数起（不因为这周才开始就断）。"""
    weeks = {week_start(x.day) for x in logs if x.done}
    at = week_start(today)
    if at not in weeks:
        at -= timedelta(days=7)
    n = 0
    while at in weeks:
        n, at = n + 1, at - timedelta(days=7)
    return n


def _sub(
    kind: Range,
    count: int,
    parts: Mapping[str, int],
    plan: Plan | None,
    logs: Sequence[WorkoutLog],
    today: date,
) -> str:
    """标题下面那一句：这周说计划几次、还有哪几次；其余说哪个部位练得最多、哪些还没练到。"""
    if kind == "week" and plan is not None:
        monday = week_start(today)
        planned = sum(plan.session_on(monday + timedelta(days=i)) is not None for i in range(7))
        left = [f"{_when(day, today)}{s.name}" for day, s in ahead(plan, logs, today)]
        if left:
            return f"计划 {planned} 次，还有{'、'.join(left)}。"
        if planned and count >= planned:
            return f"计划的 {planned} 次都练到了。"
        if planned:
            return f"计划 {planned} 次，练了 {count} 次。"
    if not count:
        return "这段时间还没有训练记录。"
    ranked = sorted(PARTS, key=lambda p: -parts[p])
    if not parts[ranked[0]]:
        return ""
    zero = [p for p in PARTS if not parts[p]]
    return f"{ranked[0]}练得最多{'，' + '、'.join(zero) + '还没练到' if zero else ''}。"


def summary(
    rows: Sequence[StoredLog],
    plan: Plan | None,
    part_of: Mapping[str, str],
    kind: Range,
    today: date,
) -> dict[str, Any]:
    """一段时间（这周、这个月、全部）练了多少，六个部位各几组、各自的动作从多重到多重，最近三次训练。"""
    start, end = span(kind, today)
    logs = [x.log for x in rows]
    done = [x for x in logs if x.done]
    mine = [x for x in done if start <= x.day <= end]
    sets = dict.fromkeys(PARTS, 0)
    for x in mine:
        for it in x.items:
            p = part_of.get(it.exercise)
            if p in sets:
                sets[p] += _n(it)
    moves = progress(logs, part_of, end)
    later = coming(plan, logs, part_of, today) if kind == "week" else {}
    count = len({x.day for x in mine})
    order = sorted(PARTS, key=lambda p: -sets[p])  # 稳定排序：一样多的照 PARTS 的顺序
    latest = sorted(
        (x for x in rows if x.log.done), key=lambda r: (r.log.day, r.created), reverse=True
    )[:3]
    return {
        "range": kind,
        "from": (start if kind != "all" else min((x.day for x in done), default=today)).isoformat(),
        "to": end.isoformat(),
        "first_day": min(x.day for x in done).isoformat() if done else None,
        "count": count,
        "sets": sum(set_count(x) for x in mine),
        "minutes": sum(x.minutes or 0 for x in mine),
        "volume_kg": round(sum(volume(x) for x in mine), 1),
        "sub": _sub(kind, count, sets, plan, logs, today),
        "streak_weeks": streak_weeks(logs, today),
        "parts": [
            {
                "part": p,
                "sets": sets[p],
                "coming": "" if sets[p] else later.get(p, ""),
                "top": moves[p][0] if moves.get(p) else None,
                "moves": moves.get(p, []),
            }
            for p in order
        ],
        "recent": [
            {
                "id": x.id,
                "day": x.log.day.isoformat(),
                "session": x.log.session or "训练",
                "minutes": x.log.minutes,
                "sets": set_count(x.log),
                "volume_kg": volume(x.log),
                "records": len(records(x.log, logs)),
                "part": top_part(x.log.items, part_of),
            }
            for x in latest
        ],
    }


def week_days(
    rows: Sequence[StoredLog],
    plan: Plan | None,
    part_of: Mapping[str, str],
    monday: date,
    today: date,
) -> dict[str, Any]:
    """肌肉图按周、按天看（§272 需求 4）：那一周七天各练了哪些动作几组。week 超出范围时收回到
    第一次训练那周和这周之间。这周还没到的计划日带上计划练的部位。"""
    logs = [x.log for x in rows]
    done = [x for x in logs if x.done]
    this = week_start(today)
    first = week_start(min((x.day for x in done), default=today))
    monday = max(first, min(this, week_start(monday)))
    later = dict(ahead(plan, logs, today)) if monday == this else {}
    days = []
    for i in range(7):
        day = monday + timedelta(days=i)
        got = [x for x in rows if x.log.done and x.log.day == day]
        items = [it for x in got for it in x.log.items]
        names = list(dict.fromkeys(x.log.session or "训练" for x in got))
        s = later.get(day)
        days.append({
            "day": day.isoformat(), "done": bool(got), "id": got[0].id if got else None, "session": "、".join(names),
            "minutes": sum(x.log.minutes or 0 for x in got), "sets": sum(set_count(x.log) for x in got),
            "part": top_part(items, part_of),
            "plan_part": top_part(s.items, part_of) if s is not None and not got else "",
            "items": [{"exercise": it.exercise, "sets": _n(it), "part": part_of.get(it.exercise, "")} for it in items if _n(it)],
        })  # fmt: skip
    return {
        "week": monday.isoformat(), "first_week": first.isoformat(), "this_week": this.isoformat(),
        "days": days, "coming": coming(plan, logs, part_of, today) if monday == this else {},
    }  # fmt: skip


def detail(row: StoredLog, rows: Sequence[StoredLog], part_of: Mapping[str, str]) -> dict[str, Any]:
    """一次训练的每一组（§272 需求 9）；破纪录的那一组标出来（算法同分享卡）。"""
    log = row.log
    broke = {
        r["exercise"]: (r["weight_kg"], r["reps"]) for r in records(log, [x.log for x in rows])
    }
    items = []
    for it in log.items:
        hit, sets = broke.get(it.exercise), []
        for s in it.performed():
            pr = hit is not None and (s.weight_kg, s.reps) == hit
            if pr:
                hit = None  # 一样的几组只标第一组
            sets.append({"weight_kg": s.weight_kg, "reps": s.reps, "seconds": s.seconds, "pr": pr})
        items.append(
            {"exercise": it.exercise, "line": it.line(), "pr": it.exercise in broke, "sets": sets}
        )
    return {
        "id": row.id, "day": log.day.isoformat(), "session": log.session or "训练", "at": log.at,
        "minutes": log.minutes, "sets": set_count(log), "volume_kg": volume(log), "rpe": log.rpe, "note": log.note,
        "part": top_part(log.items, part_of), "records": len(broke), "items": items,
    }  # fmt: skip


def eat_week(
    meals: Sequence[Any],
    monday: date,
    today: date,
    kcal_target: float | None,
    protein_target: float | None,
) -> dict[str, Any]:
    """记录页"吃的"（§272 需求 11）：那一周七天各吃了多少、每一顿是什么、在不在目标热量上下（算法同周报）。
    meals 是没删掉的饮食记录（要包括最早的，用来定能翻到哪一周）。week 超出范围时收回到第一次记饮食那周和这周之间。"""
    from ironm_agent.meals import meal_image, totals
    from ironm_agent.share import DIET_BAND

    this = week_start(today)
    first = week_start(min((m.day for m in meals), default=today))
    monday = max(first, min(this, week_start(monday)))

    def near(kcal: float) -> bool:
        return (
            bool(kcal_target) and abs(kcal - (kcal_target or 0)) <= (kcal_target or 0) * DIET_BAND
        )

    days = []
    for i in range(7):
        day = monday + timedelta(days=i)
        got = sorted(
            (m for m in meals if m.day == day), key=lambda m: (m.meal.at or "99:99", m.created)
        )
        sums = totals(got)
        days.append({
            "day": day.isoformat(), "logged": bool(got), "kcal": round(sums.kcal), "protein_g": round(sums.protein_g),
            "on_target": bool(got) and near(sums.kcal),
            "meals": [
                {"id": m.id, "kind": m.meal.kind, "name": m.meal.name, "at": m.meal.at, "kcal": round(m.meal.macros.kcal),
                 "protein_g": round(m.meal.macros.protein_g), "has_photo": m.has_photo, "image": meal_image(m.meal)}
                for m in got
            ],
        })  # fmt: skip
    return {
        "week": monday.isoformat(), "first_week": first.isoformat(), "this_week": this.isoformat(), "today": today.isoformat(),
        "kcal_target": round(kcal_target) if kcal_target else None,
        "protein_target": round(protein_target) if protein_target else None,
        "days": days,
    }  # fmt: skip
