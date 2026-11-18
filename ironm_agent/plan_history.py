"""计划哪天开始用、改过几次（spec [2026-10-23] §230）。只读，不调用模型。

计划每确认一次存一版：新排的是一条改动链的开头，在它上面按操作改的（edit）接在后面。这里把这些版本
写成给管家看的事实：档案里的一行（从哪天开始用、改过几次、最近一次是哪天、之前换过几份），
和时间线里按日期排的"换了新计划""改了计划：改了什么"。比当前这一版还新的版本是撤销掉的，不算。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ironm_agent.training import Plan, Session, StoredPlan, weekday_name

MAX_PARTS = 3  # 一次改动最多写几处
MAX_BITS = 3  # 同一天里最多写几处


def _md(d: date) -> str:
    return f"{d.month}月{d.day}日"


def _day(p: StoredPlan) -> date:
    return p.created.astimezone().date()


def _chain(versions: Sequence[StoredPlan], current: StoredPlan) -> tuple[list[StoredPlan], int, int]:
    """(到当前这一版为止的各版，这条改动链开头的位置，当前这一版的位置)；找不到当前这一版时位置是 -1。"""
    ordered = sorted((p for p in versions if p.created <= current.created), key=lambda p: p.created)
    at = next((k for k, p in enumerate(ordered) if p.id == current.id), -1)
    root = at
    while root > 0 and ordered[root].edit:
        root -= 1
    return ordered, root, at


def history_line(versions: Sequence[StoredPlan], current: StoredPlan, today: date) -> str:
    """档案里的一行：这份计划从哪天开始用、改过几次、最近一次是哪天、之前换过几份。读不到历史时为空。"""
    ordered, root, at = _chain(versions, current)
    if at < 0:
        return ""
    since = _day(ordered[root])
    days = (today - since).days
    line = f"这份从 {_md(since)} 开始用，到今天 {days} 天" if days > 0 else "这份从今天开始用"
    edits = at - root
    if edits:
        ago = (today - _day(ordered[at])).days
        last = f" {_md(_day(ordered[at]))}（{ago} 天前）" if ago > 0 else "今天"
        line += f"；之后在它上面改过 {edits} 次，最近一次是{last}"
    else:
        line += "，没改过"
    earlier = sum(1 for p in ordered[:root] if not p.edit)
    if earlier:
        line += f"；在这之前换过 {earlier} 份计划"
    return f"计划的时间：{line}。"


def _names(s: Session) -> list[str]:
    return [x.exercise for x in s.items]


def _within(day: int, old: Session, new: Session) -> str:
    """同一天的训练差在哪；没变为空。"""
    was = {x.exercise: x for x in old.items}
    now = {x.exercise: x for x in new.items}
    added = [n for n in now if n not in was]
    removed = [n for n in was if n not in now]
    bits: list[str] = []
    if old.name != new.name:
        bits.append(f"改成{new.name}")
    if len(added) == 1 and len(removed) == 1:
        bits.append(f"{removed[0]}换成{added[0]}")
    else:
        if added:
            bits.append("加了" + "、".join(added))
        if removed:
            bits.append("去掉了" + "、".join(removed))
    bits += [
        f"{n}改成 {now[n].sets}×{now[n].reps}"
        for n in now
        if n in was and (now[n].sets, now[n].reps) != (was[n].sets, was[n].reps)
    ]
    return weekday_name(day) + "、".join(bits[:MAX_BITS]) if bits else ""


def plan_diff(old: Plan, new: Plan) -> str:
    """两版计划差在哪，一句话：加了、去掉了、挪了哪天，哪天的动作换了、加了、去掉了，组数次数改了。"""
    was = {s.day: s for s in old.sessions}
    now = {s.day: s for s in new.sessions}
    added = sorted(now.keys() - was.keys())
    removed = sorted(was.keys() - now.keys())
    parts: list[str] = []
    for d in list(removed):  # 同一次训练换了一天：写挪到哪天，不写成去掉一天又加一天
        to = next((a for a in added if now[a].name == was[d].name and _names(now[a]) == _names(was[d])), None)  # fmt: skip
        if to is not None:
            parts.append(f"{weekday_name(d)}的{was[d].name}挪到{weekday_name(to)}")
            added.remove(to)
            removed.remove(d)
    parts = [f"加了{weekday_name(d)}的{now[d].name}" for d in added] + [
        f"去掉了{weekday_name(d)}的{was[d].name}" for d in removed
    ] + parts  # fmt: skip
    parts += [t for d in sorted(now.keys() & was.keys()) if (t := _within(d, was[d], now[d]))]
    if not parts:
        return "内容没变"
    return "；".join(parts[:MAX_PARTS]) + ("等" if len(parts) > MAX_PARTS else "")


def events(versions: Sequence[StoredPlan], current: StoredPlan) -> list[tuple[date, str]]:
    """时间线里的计划改动（旧的在前）：哪天换了新计划、哪天在原计划上改了什么。"""
    ordered, _, at = _chain(versions, current)
    if at < 0:
        return []
    out: list[tuple[date, str]] = []
    for k, p in enumerate(ordered):
        if p.edit and k > 0:
            out.append((_day(p), f"改了计划：{plan_diff(ordered[k - 1].plan, p.plan)}"))
        else:
            out.append((_day(p), f"换了新计划：《{p.plan.title}》每周 {p.plan.days_per_week} 练"))
    return out
