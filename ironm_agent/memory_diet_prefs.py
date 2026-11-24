"""饮食记录得出的习惯（spec [2026-10-23] §261）：和训练记录得出的偏好（memory_prefs）同一套做法——按规则算、
不调模型、不推测原因，存进记忆，来源标为"从记录看出来的"，证据写清时间范围和次数。

门槛：最近 28 天至少记了 10 天、跨 2 周以上。常吃的：至少 4 天吃过、取最多的 3 样；常点的连锁店：至少 2 天；
吃超、吃不够：只看记了 3 顿以上的日子（记不全的不算），至少 7 天，过半超过目标一成或不到八成五才写。
新规律和已有的不一样时，最近 14 天也是新规律才替换（同 memory_prefs）。暂停记忆时不算。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from ironm_agent.memory_prefs import Pref, changes_for
from ironm_agent.memory_store import MemoryStore

_log = logging.getLogger("ironm_agent.memory_prefs")

WINDOW, RECENT = 28, 14
MIN_DAYS, RECENT_MIN_DAYS = 10, 5
MIN_SPAN, RECENT_MIN_SPAN = 14, 7
FOOD_DAYS, FOOD_TOP = 4, 3
CHAIN_DAYS = 2
FULL_MEALS, FULL_DAYS = 3, 7
OVER, UNDER = 1.1, 0.85
HEAD = "饮食记录（{aspect}）："
_GRAMS = re.compile(r"\s+\d+(?:\.\d+)?\s*克$")


def _names(m: Any) -> list[str]:
    foods = [f.name for f in m.meal.foods] or [_GRAMS.sub("", x) for x in m.meal.items]
    return [x.strip() for x in foods if x and x.strip()]


def derive(meals: Sequence[Any], today: date, kcal: float | None = None, *, days: int = WINDOW,
           min_days: int = MIN_DAYS, min_span: int = MIN_SPAN) -> dict[str, Pref]:  # fmt: skip
    """最近 days 天的饮食记录 → 习惯（aspect → Pref）。记录不够时为空。"""
    from ironm_agent.chains import menus

    start = today - timedelta(days=days - 1)
    kept = [m for m in meals if getattr(m, "deleted", None) is None and start <= m.day <= today]
    logged = sorted({m.day for m in kept})
    if len(logged) < min_days or (today - logged[0]).days + 1 < min_span:
        return {}
    period = f"{start.isoformat()} 到 {today.isoformat()}，记了 {len(logged)} 天"
    out: dict[str, Pref] = {}

    def add(aspect: str, content: str, fact: str) -> None:
        out[aspect] = Pref(aspect, "diet", content, HEAD.format(aspect=aspect) + f"{period}，{fact}")

    by_food: dict[str, set[date]] = {}
    for m in kept:
        for n in _names(m):
            by_food.setdefault(n, set()).add(m.day)
    top = sorted(((len(d), n) for n, d in by_food.items() if len(d) >= FOOD_DAYS), reverse=True)[:FOOD_TOP]
    if top:
        add("diet_foods", "常吃：" + "、".join(f"{n}（{c} 天）" for c, n in top),
            "；".join(f"{n} {c} 天" for c, n in top))
    brands = [str(v.get("name") or k) for k, v in menus()[0].items()]
    chain_days = Counter()
    for b in brands:
        chain_days[b] = len({m.day for m in kept if any(n.startswith(b) for n in _names(m))})
    chains = [(c, b) for b, c in chain_days.most_common() if c >= CHAIN_DAYS]
    if chains:
        add("diet_chains", "常点：" + "、".join(f"{b}（{c} 天）" for c, b in chains),
            "；".join(f"{b} {c} 天" for c, b in chains))
    if kcal:
        per_day: dict[date, list[Any]] = {}
        for m in kept:
            per_day.setdefault(m.day, []).append(m)
        full = {d: sum(x.meal.macros.kcal for x in ms) for d, ms in per_day.items() if len(ms) >= FULL_MEALS}
        if len(full) >= FULL_DAYS:
            over = sum(v > kcal * OVER for v in full.values())
            under = sum(v < kcal * UNDER for v in full.values())
            if over * 2 > len(full):
                add("diet_balance", f"记全了的日子里多半吃超（{over}/{len(full)} 天超过热量目标一成）",
                    f"记全 {len(full)} 天，超过目标一成 {over} 天，目标约 {round(kcal)} 千卡")
            elif under * 2 > len(full):
                add("diet_balance", f"记全了的日子里多半吃不够（{under}/{len(full)} 天不到热量目标八成五）",
                    f"记全 {len(full)} 天，不到目标八成五 {under} 天，目标约 {round(kcal)} 千卡")
    return out


async def refresh(user_id: str, memory: MemoryStore, meals: Any, kcal: float | None = None,
                  today: date | None = None) -> list[Any]:  # fmt: skip
    """按最新的饮食记录更新饮食习惯；暂停记忆时不更新。日志只记条数，不记内容。"""
    today = today or date.today()
    if await memory.paused(user_id):
        return []
    rows = await meals.since(user_id, today - timedelta(days=WINDOW - 1))
    current = derive(rows, today, kcal)
    recent = derive(rows, today, kcal, days=RECENT, min_days=RECENT_MIN_DAYS, min_span=RECENT_MIN_SPAN)
    existing = [x for x in await memory.items(user_id) if x.evidence.startswith("饮食记录（")]
    changes = changes_for(existing, current, recent)
    if changes:
        await memory.apply(user_id, changes)
    _log.info("diet prefs refreshed: derived=%d changes=%d", len(current), len(changes))
    return changes
