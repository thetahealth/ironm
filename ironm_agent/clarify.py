"""排训练前先确认条件（spec [2026-10-23] §55、§143）：要不要排、要不要先问由作答的模型判断（调 ask_terms）；
这里只提供列条件的固定句式（目标、场景、器械、时间、伤病，有旧答案的带上来源和日期，没变就回「按这个排」）
和认出"要计划"说法的正则（只用来兜底核对，不用来做决定）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta

from ironm_agent.memory_store import Item
from ironm_agent.memory_view import EVENT_DAYS, Recall
from ironm_agent.metrics import GOALS, Profile

PLAN_REQUEST = re.compile(
    r"(?:计划|课表|安排|排一?下|排个|排一个|制定|定制|设计).{0,8}(?:训练|练|健身|一周|每周|周)"
    r"|(?:训练|健身|练).{0,6}(?:计划|课表)"
    r"|(?:今天|今晚|明天|晚上|待会)(?:该|要|应该)?(?:练什么|怎么练|练啥|练哪|练点什么)"
    r"|(?:给我|来|要|想要|定|做|排|出|制定|重新定|重新做)[^，。？！?!]{0,8}(?:计划|课表)"
)
# 问的是有多少、有哪些计划（§83），不是要一份：没说"给我排、帮我做、要一份"时不当成要新计划
ASK_ABOUT_PLANS = re.compile(
    r"(?:多少|几个|几份|几套|哪些|什么样的|有什么|都有|还有别的|别的|其他)[^，。？！?!]{0,6}(?:训练)?(?:计划|方案|课表)"
    r"|(?:计划|方案|课表)[^，。？！?!]{0,4}(?:有多少|有几个|有几份|有哪些|都有什么|多不多)"
)
_ASK_FOR_ONE = re.compile(r"给我|帮我|替我|排|做一份|来一份|要一份|制定|定制|设计|换")
# 说的不是训练计划
_NOT_TRAINING = re.compile(r"饮食|食谱|吃|餐|营养|减脂餐|热量|睡眠|作息")
_HURT = re.compile(r"疼|痛|伤|膝|腰|肩|好了|恢复")
MARK = "也可以直接说「你定」"  # 追问的标志：本会话已经问过就不再问
HEAD = "排新计划前先确认一下"  # 确认五项的开头（之前的回答只留开头一段，标志放开头才找得到）
CONFIRM = "都没变就回「按这个排」"
DEFAULT_PLAN = "每周 3 天、每次 45 分钟、徒手加哑铃"
CONFIRMED_NOTE = (
    "上一轮你列了排新计划要用的条件请他确认（见前几轮对话）。先看他这句是不是在回应那张清单（确认、改了几项、「你定」）："
    "是的话，列出的条件里他说变了的按新的排，其余照列出的排；只说「你定」时，有旧答案的照旧，没有的按默认值（"
    + DEFAULT_PLAN + "）；计划交给预览卡片显示，回答先答他这句问的，再用一两句说这份计划怎么为他的目标排，他说了的变化复述一句（§182）。"
    "他问的是别的事（比如问身体、问吃什么），就只答他问的，这一轮不写计划（§108）。"
)

_PLACES = (("健身房", "健身房"), ("家", "在家"), ("宿舍", "宿舍"), ("酒店", "酒店"),
           ("办公室", "办公室"), ("操场", "户外"), ("公园", "户外"), ("户外", "户外"))  # fmt: skip


def _md(d: date) -> str:
    return f"{d.month}月{d.day}日"


def _said(items: Sequence[Item]) -> str:
    """几条记忆连起来，带上最早那条是哪天说的。"""
    first = min(x.created for x in items)
    return "；".join(x.content for x in items) + f"（{_md(first.date())}你说的）"


_ONE_DAY = re.compile(r"今天|今晚|今早|今日|今儿")
_THIS_WEEK = re.compile(r"这周|本周|这个星期|这礼拜")
_NEXT_WEEK = re.compile(r"下周|下个星期|下礼拜")


def _until(x: Item) -> date:
    """临时情况到哪天为止：说了今天的只算那天，说了这周、下周的到那周日，其余按两周。"""
    start = x.created.date()
    if _ONE_DAY.search(x.content):
        return start
    sunday = start + timedelta(days=6 - start.weekday())
    if _NEXT_WEEK.search(x.content):
        return sunday + timedelta(days=7)
    if _THIS_WEEK.search(x.content):
        return sunday
    return start + timedelta(days=EVENT_DAYS)


def _span(start: date, end: date) -> str:
    return f"{_md(start)}当天" if end == start else f"{_md(start)}起，到{_md(end)}"


def conditions(
    recall: Recall,
    profile: Profile,
    *,
    target: str = "",
    plan_line: str = "",
    today: date | None = None,
    checked_today: bool = False,
) -> list[str]:
    """排新计划前要确认的五项（§55 需求 1、2、6、7）：有旧答案的带上来源和日期，没有的只问一句。

    target：目标表里的目标（"2027年10月26日前体重到 65 公斤，现在 67 公斤（10月26日设的）"）；
    plan_line：当前计划的时间安排（"现在的计划每周 4 练：周一、周二、周四、周六"）。
    """
    active = [x for x in recall.items if x.status != "resolved"]

    def of(cat: str) -> list[Item]:
        return [x for x in active if x.category == cat]

    out = []
    goals = of("goal")
    if target:
        out.append(f"①目标：{target}")
    elif goals:
        out.append(f"①目标：{_said(goals)}")
    elif profile.goal:
        out.append(
            f"①目标：档案里写的是{GOALS[profile.goal]}，有具体的数和日期吗（比如几月前减到多少公斤）？"
        )
    else:
        out.append("①目标：主要想增肌、减脂，还是提升体能？有具体的数和日期更好。")
    gear = of("equipment")
    places = [x for x in gear if any(k in x.content for k, _ in _PLACES)]
    if places:
        names = list(dict.fromkeys(n for x in places for k, n in _PLACES if k in x.content))
        out.append(
            f"②场景：{'、'.join(names)}（{_md(min(x.created for x in places).date())}你说的）"
        )
    else:
        out.append("②场景：在家、健身房，还是户外？")
    out.append(
        f"③器械：{_said(gear)}" if gear else "③器械：手边有什么器械（比如哑铃多重），还是徒手？"
    )
    times = of("schedule")
    if times:
        out.append(f"④时间：{_said(times)}" + (f"；{plan_line}" if plan_line else ""))
    elif plan_line:
        out.append(f"④时间：{plan_line}")
    else:
        out.append("④时间：一周能练几天、每次大概多久、哪几天有空？")
    hurts = of("injury") + of("special")  # 孕期、高血压这类特殊情况也在这一项确认（§148）
    if hurts:
        out.append(f"⑤伤病：{_said(hurts)}，现在怎么样？")
    else:
        out.append("⑤伤病：有没有旧伤、正在恢复或者哪里不能练？")
    # 临时的和上面五项长期的分开写，带起止日期；已经过了的不写（§55 需求 7）
    temp = [(x, _until(x)) for x in of("event")]
    temp = [(x, end) for x, end in temp if today is None or end >= today]
    if temp:
        spans = [f"{x.content}（{_span(x.created.date(), end)}）" for x, end in temp]
        out.append("这两周的临时条件（到期就不再用）：" + "；".join(spans))
    if today is not None:
        tomorrow = today + timedelta(days=1)
        out.append(
            f"今天已经打过卡，新计划从明天（{_md(tomorrow)}）开始。"
            if checked_today
            else f"新计划今天开始还是明天（{_md(tomorrow)}）开始？不说就从今天开始。"
        )
    return out


def confirm_ask(lines: Sequence[str]) -> str:
    return (
        f"{HEAD}，我打算按这些排：\n"
        + "\n".join(lines)
        + f"\n\n{CONFIRM}，有变的说哪几项（是这两周临时的还是以后都这样也说一声）；{MARK}。"
    )


