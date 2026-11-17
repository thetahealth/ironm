"""按七步算出一份训练计划草稿（spec [2026-10-23] §216、§217）。不调模型、不随机：同样的输入每次一样。

1 分化结构（每周几天、水平）→ 2 每周组数（目标、水平、想重点练哪、时间）→ 3 动作位（动作类型 + 角色）
→ 4 挑动作（器械、伤病、喜好、练过什么、难度）→ 5 剂量（目标、水平、角色）→ 6 阶段（8 周一个周期）
→ 7 说明（每一步为什么）。检查由调用的地方照旧用 plan_check 做。

表里的每个数在 SOURCES 里写出处：依据库编号，或〔经验〕（依据库只有方向、没有具体数；待审定）。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from functools import cache
from itertools import combinations, pairwise
from typing import Literal

from ironm_agent import body
from ironm_agent.exercises import Equipment
from ironm_agent.protools.movement import (
    COMPOUND,
    JOINT_NAMES,
    PATTERN_NAMES,
    RELATED,
    Move,
    equipment_ok,
    moves,
    stressed,
)
from ironm_agent.protools.plan_check import MAJOR, SLACK, back_to_back, estimate_minutes
from ironm_agent.training import Plan, PlanItem, Session, weekday_name

type Level = Literal["none", "lt6m", "6m-2y", "2y+"]
type Goal = Literal["health", "fat_loss", "muscle", "strength", "endurance"]
type Role = Literal["main", "second", "iso", "core"]

NOVICE: frozenset[str] = frozenset({"none", "lt6m"})
GOAL_NAMES = {"health": "健康体能", "fat_loss": "减脂", "muscle": "增肌", "strength": "力量", "endurance": "耐力"}
LEVEL_NAMES = {"none": "零基础", "lt6m": "练了不到 6 个月", "6m-2y": "练了 6 个月到 2 年", "2y+": "练了 2 年以上"}
ISOLATION: frozenset[str] = frozenset({"shoulder", "curl", "extension", "calf", "abduction"})
CYCLE = 8  # 一个周期几周，最后一周减量
NOVICE_DAYS = 3  # 新手每周最多几次力量
MIN_SETS = 4  # 每个主要部位每周最少几组
FOCUS_EXTRA = 3  # 想重点练的部位每周多几组
DEFAULT_DAYS: dict[int, tuple[int, ...]] = {
    1: (3,), 2: (1, 4), 3: (1, 3, 5), 4: (1, 2, 4, 5), 5: (1, 2, 3, 5, 6), 6: (1, 2, 3, 4, 5, 6),
}  # fmt: skip

SOURCES: dict[str, str] = {
    "新手每周 2–3 次全身、最多 3 次力量": "K03",
    "每个主要部位每周至少练到 2 次": "K05",
    "同一部位两次之间隔一天": "K48",
    "4 天上下肢、5 天上下肢加推拉腿、6 天推拉腿两轮": "〔经验〕",
    "每个部位每周至少 4 组": "K61",
    "有基础的人增肌每周 10 组以上": "K04",
    "减脂时力量部分照增肌排": "K37、K87",
    "新手每周 6 组左右、有基础 10–12 组的目标数": "〔经验〕",
    "想重点练的部位每周多 3 组": "〔经验〕",
    "新手每组 8–12 次、每个动作 1–3 组": "K03",
    "增肌以 6–12 次为主": "K03",
    "力量主项 3–5 次、第二个多关节动作 6–8 次": "K30、〔经验〕",
    "孤立动作 10–15 次": "〔经验〕",
    "每组做完还剩几次": "K28",
    "主项组间休息 2–3 分钟、其余 1–2 分钟": "K03",
    "8 周一个周期、最后一周减量、组数减半重量略降": "K31",
    "新手头两周每个动作少做一组": "〔经验〕",
    "时间少时先保多关节动作": "K61",
}

# 3 动作位：每种训练有哪几个位置（动作类型，角色）。主项在前，孤立动作在后。
SESSIONS: dict[str, tuple[tuple[str, Role], ...]] = {
    # 全身的每一次都要练到胸、背、腿臀、肩：每周只练 2 次时每个部位也能练到 2 次（K05）
    "全身 A": (("squat", "main"), ("h_push", "main"), ("h_pull", "main"), ("v_push", "second"),
             ("bridge", "second"), ("core", "core")),
    "全身 B": (("hinge", "main"), ("v_pull", "main"), ("h_push", "main"), ("v_push", "second"),
             ("lunge", "second"), ("core", "core")),
    "全身 C": (("squat", "main"), ("h_pull", "main"), ("v_push", "main"), ("h_push", "second"),
             ("hinge", "second"), ("core", "core")),
    "上肢 A": (("h_push", "main"), ("h_pull", "main"), ("v_push", "second"), ("v_pull", "second"),
             ("h_push", "second"), ("curl", "iso"), ("extension", "iso")),
    "上肢 B": (("v_pull", "main"), ("h_push", "main"), ("h_pull", "second"), ("v_push", "second"),
             ("shoulder", "iso"), ("extension", "iso"), ("curl", "iso")),
    "下肢 A": (("squat", "main"), ("hinge", "main"), ("lunge", "second"), ("bridge", "second"),
             ("calf", "iso"), ("core", "core")),
    "下肢 B": (("hinge", "main"), ("squat", "main"), ("bridge", "second"), ("lunge", "second"),
             ("abduction", "iso"), ("core", "core")),
    "推": (("h_push", "main"), ("v_push", "main"), ("h_push", "second"), ("shoulder", "iso"),
          ("extension", "iso"), ("core", "core")),
    "拉": (("v_pull", "main"), ("h_pull", "main"), ("h_pull", "second"), ("shoulder", "iso"),
          ("curl", "iso"), ("core", "core")),
    "腿": (("squat", "main"), ("hinge", "main"), ("lunge", "second"), ("bridge", "second"),
          ("calf", "iso"), ("core", "core")),
    "推 B": (("v_push", "main"), ("h_push", "main"), ("h_push", "second"), ("shoulder", "iso"),
            ("extension", "iso"), ("core", "core")),
    "拉 B": (("h_pull", "main"), ("v_pull", "main"), ("v_pull", "second"), ("shoulder", "iso"),
            ("curl", "iso"), ("core", "core")),
    "腿 B": (("hinge", "main"), ("squat", "main"), ("bridge", "second"), ("lunge", "second"),
            ("abduction", "iso"), ("core", "core")),
}  # fmt: skip
# 1 分化结构：每周几次力量 → 叫什么、依次练哪几种
SPLITS: dict[int, tuple[str, tuple[str, ...]]] = {
    1: ("全身", ("全身 A",)),
    2: ("全身", ("全身 A", "全身 B")),
    3: ("全身", ("全身 A", "全身 B", "全身 C")),
    4: ("上下肢", ("上肢 A", "下肢 A", "上肢 B", "下肢 B")),
    5: ("上下肢加推拉腿", ("上肢 A", "下肢 A", "推", "拉", "腿 B")),
    6: ("推拉腿", ("推", "拉", "腿", "推 B", "拉 B", "腿 B")),
}
# 2 每周组数：每个主要部位的目标数（时间够时补到这个数）
TARGET: dict[str, dict[str, int]] = {
    "none": {"health": 6, "fat_loss": 6, "muscle": 6, "strength": 6, "endurance": 6},
    "lt6m": {"health": 6, "fat_loss": 6, "muscle": 6, "strength": 6, "endurance": 6},
    "6m-2y": {"health": 8, "fat_loss": 10, "muscle": 10, "strength": 10, "endurance": 8},
    "2y+": {"health": 8, "fat_loss": 12, "muscle": 12, "strength": 12, "endurance": 8},
}
# 5 剂量：（目标的排法，角色）→ 次数。新手不分目标。
REPS: dict[str, dict[Role, str]] = {
    "novice": {"main": "8–12", "second": "8–12", "iso": "10–15", "core": "10–15"},
    "muscle": {"main": "6–10", "second": "8–12", "iso": "10–15", "core": "10–15"},
    "strength": {"main": "3–5", "second": "6–8", "iso": "10–15", "core": "10–15"},
    "health": {"main": "8–12", "second": "8–12", "iso": "10–15", "core": "10–15"},
    "endurance": {"main": "12–15", "second": "12–15", "iso": "12–15", "core": "10–15"},
}
BASE_SETS: dict[Role, int] = {"main": 3, "second": 3, "iso": 2, "core": 2}
NOVICE_SETS: dict[Role, int] = {"main": 3, "second": 2, "iso": 2, "core": 2}
MAX_SETS: dict[Role, int] = {"main": 4, "second": 4, "iso": 3, "core": 3}
NOVICE_CAP = 3  # 新手每个动作最多几组（§79）
_EASY = {"器械": 0, "哑铃": 1, "壶铃": 1, "弹力带": 1, "自重": 1, "杠铃": 2, "单杠": 2, "泡沫轴": 3}  # 新手先用稳的
# 有基础的人，主项先用能加重的器械（〔经验〕）；练力量时杠铃排最前
_LOAD = {"杠铃": 0, "器械": 0, "单杠": 0, "哑铃": 1, "壶铃": 2, "弹力带": 3, "自重": 3, "泡沫轴": 3}
_HEAVY = {"杠铃": 0, "器械": 1, "单杠": 1, "哑铃": 2, "壶铃": 3, "弹力带": 4, "自重": 4, "泡沫轴": 4}


# 每种训练主要练哪几个部位：挑动作时优先只练这些部位的，免得第二天练别的部位时又练到（同一部位不连着两天）
KIND_PARTS: dict[str, frozenset[str]] = {
    "全": frozenset(MAJOR), "上": frozenset({"胸", "背", "肩"}), "下": frozenset({"股四头肌", "臀"}),
    "推": frozenset({"胸", "肩"}), "拉": frozenset({"背"}), "腿": frozenset({"股四头肌", "臀"}),
}  # fmt: skip


@cache
def _staples() -> dict[str, int]:
    """方案库里手写过的动作有多"基础"：在一次训练的前两个位置、而且是首选的记 0；前两个位置的备选记 1；
    只在后面的位置出现过的记 2；没出现过的不在表里（按 3 算）。主项只用 0、1 的：只按动作类型挑，
    会把夹胸、腿屈伸、腿弯举这类单关节动作挑成主项。"""
    from ironm_agent.protools.templates import templates

    out: dict[str, int] = {}
    for t in templates():
        if t.kind != "base":
            continue
        for slots in t.sessions.values():
            for at, slot in enumerate(slots):
                for i, name in enumerate(slot.pick):
                    tier = 2 if at > 1 else 0 if i == 0 else 1
                    out[name] = min(out.get(name, 3), tier)
    return out


@dataclass(frozen=True)
class Built:
    plan: Plan
    structure: str
    why: tuple[str, ...]  # 七步各一句
    notes: tuple[str, ...]  # 减了什么、空了哪个位置、周期怎么走
    sets: dict[str, int]  # 每个主要部位每周几组（常用名）


@dataclass
class _Slot:
    pattern: str
    role: Role
    move: Move
    sets: int


def _dose(goal: str | None, level: str | None) -> str:
    """这个人用哪一套次数：新手不分目标（K03）；减脂照增肌排（K37、K87）。"""
    if level is None or level in NOVICE:
        return "novice"
    return {"fat_loss": "muscle", "muscle": "muscle", "strength": "strength", "endurance": "endurance"}.get(
        goal or "", "health"
    )


def _groups(move: Move) -> list[str]:
    return [body.name(code) for code in move.tag.groups]


def _hit(text: str, names: Collection[str]) -> bool:
    return any(n and (n in text or text in n) for n in names)


def _pick(pattern: str, role: Role, kind: str, *, novice: bool, equipment: frozenset[Equipment] | None,
          injured: Collection[str], prefer: Collection[str], avoid: Collection[str], done: Collection[str],
          used: Collection[str], here: Collection[str] = (), dose: str = "novice") -> Move | None:  # fmt: skip
    """4 挑动作：同类型的全部动作里，筛掉做不了的、不要的，再按喜欢、练过、只练本部位、基础动作、常见、好学排。
    主项只从方案库里手写过的基础动作里挑（有的话）。"""
    staples = _staples()
    parts = KIND_PARTS[kind[0]]
    # 先找这一周还没用过的同类动作；没有就允许和别的天重复（同一次训练里不重复）；再没有才到相近的类型里找
    for kinds, taken in (((pattern,), {*used, *here}), ((pattern,), set(here)),
                         (RELATED.get(pattern, ()), {*used, *here}), (RELATED.get(pattern, ()), set(here))):  # fmt: skip
        found: list[tuple[tuple[int, ...], Move]] = []
        for order, m in enumerate(moves()):
            name = m.exercise.name
            if (m.tag.pattern not in kinds or name in taken or not equipment_ok(m, equipment)
                    or stressed(m, injured) or _hit(name, avoid)
                    or (novice and m.exercise.level != "入门")):  # fmt: skip
                continue
            ease = min((_EASY.get(e, 3) for e in m.exercise.equipment), default=3)
            off = sum(g in MAJOR and g not in parts for g in _groups(m))
            basic = staples.get(name, 3) if role in ("main", "second") else 0
            table = _HEAVY if dose == "strength" else _LOAD
            load = min((table.get(e, 4) for e in m.exercise.equipment), default=4) if role == "main" else 0
            key = (0 if _hit(name, prefer) else 1, 0 if name in done else 1, off, basic,
                   ease if novice else load, 0 if m.exercise.common else 1, order)  # fmt: skip
            found.append((key, m))
        if role == "main" and any(staples.get(m.exercise.name, 3) < 2 for _, m in found):
            found = [x for x in found if staples.get(x[1].exercise.name, 3) < 2]
        if found:
            return min(found, key=lambda t: t[0])[1]
    return None


def _reps(slot: _Slot, dose: str) -> str:
    if slot.move.exercise.timed:
        return "30–45 秒"
    role: Role = slot.role
    if dose == "strength" and role == "main" and slot.move.tag.pattern in ("h_pull", "v_pull"):
        role = "second"  # 练力量时少次数只用在蹲、硬拉、推上，划船和下拉照 6–8 次（〔经验〕，同方案库的力量方案）
    if dose == "strength" and role == "main" and min(
        (_HEAVY.get(e, 4) for e in slot.move.exercise.equipment), default=4
    ) > 1:
        role = "second"  # 不是杠铃、器械的主项（哑铃、壶铃、自重）不用 3–5 次
    reps = REPS[dose][role]
    if slot.move.tag.pattern == "lunge":
        return f"每侧 {reps.split('–')[0]}–{int(reps.split('–')[0]) + 2} 次"
    return reps


def _session(day: int, name: str, slots: Sequence[_Slot], dose: str, injured: Collection[str]) -> Session:
    items = [PlanItem(exercise=s.move.exercise.name, sets=s.sets, reps=_reps(s, dose)) for s in slots]
    draft = Session(day=day, name=name, minutes=10, items=items)
    return draft.model_copy(update={"minutes": max(10, min(180, round(estimate_minutes(draft))))})


def _minutes(slots: Sequence[_Slot], dose: str) -> float:
    return estimate_minutes(_session(1, "x", slots, dose, ()))


def _trim(slots: list[_Slot], minutes: int, dose: str) -> list[str]:
    """时间不够按重要性减（§216 需求 4）：孤立动作的组数 → 去掉孤立动作 → 核心、单腿、臀桥的组数（每次留一个核心）
    → 去掉臀桥类 → 多关节动作的组数（辅助的先减）→ 核心减到 1 组 → 还不够才去掉辅助的多关节动作。
    多关节的主项不整个去掉。返回减了什么。"""
    cut: list[str] = []

    def over() -> bool:
        return _minutes(slots, dose) > minutes * SLACK

    def lower(pick: Sequence[_Slot], floor: int) -> None:
        for s in reversed(pick):
            while over() and s.sets > floor:
                s.sets -= 1
                cut.append(f"{s.move.exercise.name}减到 {s.sets} 组")

    lower([s for s in slots if s.role == "iso"], 2)
    for s in [s for s in reversed(slots) if s.role == "iso"]:
        if over():
            slots.remove(s)
            cut.append(f"去掉{s.move.exercise.name}")
    lower([s for s in slots if s.role == "core" or s.pattern in ("lunge", "bridge")], 2)
    for s in [s for s in reversed(slots) if s.role == "second" and s.pattern not in COMPOUND]:
        if over():  # 臀桥类不算多关节主项：时间实在不够时去掉
            slots.remove(s)
            cut.append(f"去掉{s.move.exercise.name}")
    lower([s for s in slots if s.pattern in COMPOUND and s.role == "main"], 2)
    lower([s for s in slots if s.pattern in COMPOUND and s.role != "main"], 2)
    lower([s for s in slots if s.role == "core"], 1)
    for s in [s for s in reversed(slots) if s.role == "second" and s.pattern in COMPOUND]:
        if over():  # 别的都减完了还超时：去掉辅助的多关节动作，主项留着
            slots.remove(s)
            cut.append(f"去掉{s.move.exercise.name}")
    lower([s for s in slots if s.role == "main"], 1)  # 实在排不下：主项从后往前减到 1 组（每个动作 1–3 组，K03）
    return cut


def _weekly(week: Sequence[Sequence[_Slot]]) -> dict[str, int]:
    out = dict.fromkeys(MAJOR, 0)
    for slots in week:
        for s in slots:
            for g in _groups(s.move):
                if g in out:
                    out[g] += s.sets
    return out


def _fill(week: list[list[_Slot]], target: dict[str, int], minutes: int | None, dose: str, cap: dict[Role, int]) -> None:
    """2 每周组数：哪个主要部位没到目标数，就给练它的位置加组（主项先加），时间装得下才加。"""
    for _ in range(40):
        sets = _weekly(week)
        short = sorted((g for g in MAJOR if sets[g] < target[g]), key=lambda g: sets[g] - target[g])
        added = False
        for g in short:
            places = [(slots, s) for slots in week for s in slots if g in _groups(s.move) and s.sets < cap[s.role]]
            places.sort(key=lambda t: (t[1].sets, t[1].role != "main"))
            for slots, s in places:
                s.sets += 1
                if minutes is not None and _minutes(slots, dose) > minutes * SLACK:
                    s.sets -= 1
                    continue
                added = True
                break
            if added:
                break
        if not added:
            return


def _phase(sets: int, role: Role, week: int, novice: bool) -> int:
    """6 阶段：新手头两周每个动作少一组；最后一周减量、组数减半（K31）。"""
    if week >= CYCLE:
        return max(1, (sets + 1) // 2)
    if novice and week <= 2:  # 只减不加：已经因为时间减到底的不动
        return min(sets, max(1 if role in ("iso", "core") else 2, sets - 1))
    return sets


def _place(n: int, weekdays: Sequence[int], kinds: Sequence[str], make: object) -> tuple[int, ...]:
    """排到周几：同一部位不连着两天的最少、训练日隔得最开；他说了哪几天就在那几天里排。"""
    pool = sorted(set(weekdays)) if len(set(weekdays)) >= n else list(range(1, 8))
    best: tuple[tuple[int, int, int], tuple[int, ...]] | None = None
    for combo in combinations(pool, n):
        plan = make(combo)  # type: ignore[operator]
        ds = sorted(combo)
        gap = min([b - a for a, b in pairwise(ds)] + [ds[0] + 7 - ds[-1]]) if n > 1 else 7
        key = (len(back_to_back(plan)), -gap, 0 if combo == DEFAULT_DAYS.get(n) else 1)
        if best is None or key < best[0]:
            best = (key, combo)
    assert best is not None
    return best[1]


def build(
    *,
    level: Level | None,
    goal: Goal | None,
    days: int,
    minutes: int | None = None,
    place: Literal["gym", "home"] | None = None,
    equipment: frozenset[Equipment] | None = None,
    injured: Collection[str] = (),
    weekdays: Sequence[int] = (),
    prefer: Collection[str] = (),
    avoid: Collection[str] = (),
    focus: Collection[str] = (),
    done: Collection[str] = (),
    week: int = 1,
) -> Built:
    """按七步算出这一周的计划草稿。算不出来（没有可用的动作）抛 ValueError。"""
    novice = level is None or level in NOVICE
    dose = _dose(goal, level)
    week = max(1, min(week, CYCLE))
    notes: list[str] = []

    # 1 分化结构
    n = max(1, min(days, 6))
    if novice and n > NOVICE_DAYS:
        n = NOVICE_DAYS
        notes.append(f"新手每周最多排 {NOVICE_DAYS} 次力量，你说的每周 {days} 天里，其余的天留给有氧或休息")
    elif days > 6:
        notes.append("每周最多排 6 次力量，留一天休息")
    structure, kinds = SPLITS[n]

    # 3 动作位 + 4 挑动作
    used: list[str] = []
    missing: list[str] = []
    swapped: list[str] = []
    week_slots: list[list[_Slot]] = []
    base = NOVICE_SETS if novice else BASE_SETS
    for kind in kinds:
        slots: list[_Slot] = []
        for pattern, role in SESSIONS[kind]:
            m = _pick(pattern, role, kind, novice=novice, equipment=equipment, injured=injured, prefer=prefer,
                      avoid=avoid, done=done, used=used, here=[s.move.exercise.name for s in slots],
                      dose=dose)  # fmt: skip
            if m is None:
                missing.append(f"{kind}的{PATTERN_NAMES[pattern]}")
                continue
            if m.tag.pattern != pattern:
                swapped.append(f"{kind}的{PATTERN_NAMES[pattern]}换成相近的「{m.exercise.name}」")
            used.append(m.exercise.name)
            slots.append(_Slot(pattern, role, m, base[role]))
        if not any(s.move.tag.pattern in COMPOUND for s in slots):
            raise ValueError(f"{kind}排不出多关节动作")
        week_slots.append(slots)

    # 2 每周组数
    cap = {r: min(v, NOVICE_CAP) for r, v in MAX_SETS.items()} if novice else dict(MAX_SETS)
    goal_key = goal or "health"
    target = dict.fromkeys(MAJOR, TARGET[level or "none"][goal_key])
    wanted = [g for g in MAJOR if _hit(g, focus)]
    cuts: list[str] = []
    if minutes is not None:
        for kind, slots in zip(kinds, week_slots, strict=True):
            cut = _trim(slots, minutes, dose)
            if cut:
                cuts.append(f"{kind}按 {minutes} 分钟减：" + "、".join(cut))
            if _minutes(slots, dose) > minutes * SLACK:
                notes.append(f"{kind}减到最少还是比 {minutes} 分钟长，这个时间排不下：每次多留一点时间，或者少排一个部位")
    _fill(week_slots, target, minutes, dose, cap)
    if wanted:  # 想重点练的部位：在已经排到的量上再多几组
        now = _weekly(week_slots)
        _fill(week_slots, {g: (now[g] + FOCUS_EXTRA if g in wanted else 0) for g in MAJOR}, minutes, dose, cap)
    full = _weekly(week_slots)
    low = [g for g in MAJOR if full[g] < MIN_SETS]
    if low:
        notes.append("这样排下来" + "、".join(f"{g}每周 {full[g]} 组" for g in low)
                     + f"，不到 {MIN_SETS} 组（K61）：每次多练一会儿，或者加一天")  # fmt: skip

    # 6 阶段：这一周实际做几组
    for slots in week_slots:
        for s in slots:
            s.sets = _phase(s.sets, s.role, week, novice)

    # 排到周几，做成计划
    def make(combo: Sequence[int]) -> Plan:
        sessions = [_session(d, k, slots, dose, injured) for d, k, slots in zip(combo, kinds, week_slots, strict=True)]
        return Plan(title="按你的情况排", goal=GOAL_NAMES.get(goal_key, "") if goal else "", days_per_week=n,
                    sessions=sessions)  # fmt: skip

    combo = _place(n, weekdays, kinds, make)
    plan = make(combo)
    if weekdays and len(set(weekdays)) < n:
        notes.append(f"你说能练的日子不到 {n} 天，先按常见排法放，具体哪天可以再挪")
    notes += cuts + [f"{x}（没有合适的同类动作）" for x in swapped]
    notes += [f"{x}这一位按你的器械和伤病找不到合适的动作，先空着" for x in missing]
    if week >= CYCLE:
        stage = f"现在是第 {week} 周，减量周：组数减半、重量略降，下周开始新的 {CYCLE} 周"
    elif novice and week <= 2:
        stage = f"现在是第 {week} 周，先学动作：每个动作比正常少做一组，第 3 周起加回来"
    else:
        stage = f"现在是第 {week} 周，照正常量练；第 {CYCLE} 周减量"
    notes.append(f"{CYCLE} 周一个周期：{stage}（K31）")

    # 7 说明：七步各一句
    sets = _weekly([[_Slot(s.pattern, s.role, s.move, s.sets) for s in slots] for slots in week_slots])
    who = LEVEL_NAMES.get(level or "", "水平不知道，按新手")
    hurt = "、".join(JOINT_NAMES.get(j, j) for j in injured)
    liked = [nm for nm in used if _hit(nm, prefer)]
    did = [nm for nm in used if nm in done and nm not in liked]
    lines = {
        "novice": "每组 8–12 次、每个动作不超过 3 组，不因目标变（K03）",
        "muscle": "主项 6–10 次、辅助 8–12 次（K03）" + ("；减脂时力量部分照增肌排，才保得住肌肉（K37、K87）" if goal == "fat_loss" else ""),
        "strength": "主项 3–5 次、第二个多关节动作 6–8 次，以大重量、少次数为主（K30；具体次数〔经验〕）",
        "health": "每组 8–12 次（K03）",
        "endurance": "每组 12–15 次（〔经验〕）",
    }  # fmt: skip
    why = (
        f"结构：每周 {days} 天、{who}，排{structure}，每周 {n} 次（{'、'.join(weekday_name(d) for d in combo)}）；"
        f"每个主要部位每周练到 2 次（K05），同一部位不连着两天（K48）"
        + ("；新手每周 2–3 次全身（K03）" if novice else "；4 天以上的分法〔经验〕" if n >= 4 else ""),
        f"每周组数：目标是{GOAL_NAMES.get(goal_key, '健康体能')}、{who}，每个主要部位每周以 {TARGET[level or 'none'][goal_key]} 组为目标"
        f"（下限 {MIN_SETS} 组 K61，有基础的增肌每周 10 组以上 K04，目标数〔经验〕）"
        + (f"；你想重点练{'、'.join(wanted)}，每周多排 {FOCUS_EXTRA} 组〔经验〕" if wanted else "")
        + "；现在是：" + "、".join(f"{g} {sets[g]} 组" for g in MAJOR),
        "动作位：每次先做多关节的主项（蹲、髋铰链、推、拉），再做辅助和孤立动作，最后核心；时间少时先保多关节动作（K61）",
        "挑动作：从动作库里同类型的动作里挑，"
        + (f"器械按你有的（{'、'.join(sorted(equipment))}）" if equipment is not None else "器械不知道、没限制")
        + (f"，避开给{hurt}加负担的动作" if hurt else "")
        + ("，新手只用入门难度、先用好学的器械〔经验〕" if novice else "")
        + (f"；你喜欢的排进去了：{'、'.join(liked)}" if liked else "")
        + (f"；最近练过的接着用：{'、'.join(did[:4])}" if did else "")
        + (f"；按你说的不排：{'、'.join(sorted(avoid))}" if avoid else "")
        + "（动作的标注待审定，〔经验〕）",
        f"剂量：{lines[dose]}；每组做完还剩 2–3 次（K28），主项组间休息 2–3 分钟、其余 1–2 分钟（K03）",
        f"阶段：{stage}（K31）" + ("；新手头两周少做一组〔经验〕" if novice else ""),
        "检查：算出来的这一份照计划检查的规则查过（每周组数、次数、连着两天、伤病、器械、时长），"
        "表里没有依据的数标了〔经验〕、待审定",
    )  # fmt: skip
    return Built(plan, structure, why, tuple(notes), sets)
