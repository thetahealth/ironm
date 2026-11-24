"""单 Agent（spec [2026-10-23] §67）：一个 Agent 回答所有问题，规程（Skill）按需装载，工具由它按需调用。

作答循环（查询次数上限、逐句核对出处和数字、不合格改一次、再不合格修正）在 experts.py；
规程目录每份一行，用到再装（§255、§263）；可以查 App 用法；要不要改计划、排新计划、记训练由它看上下文判断，写在回答的 JSON 里，由系统执行。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from typing import Any

from ironm_agent.actions import AFTER, ALWAYS, ASK_TERMS, LOG_WORKOUT, NEW_PLAN, PLAN_EDIT
from ironm_agent.answer_format import slots_prompt
from ironm_agent.casefile import CaseFile
from ironm_agent.coach import app_guide
from ironm_agent.experts import (
    Consultation,
    Opinion,
    _loop,
    _user_message,
    experience_share,
)
from ironm_agent.frameworks import FRAMEWORKS, flag_refs, flags_of, framework_refs
from ironm_agent.llm import LLM, ModelUnavailable
from ironm_agent.memory import Conversation, conversation_text
from ironm_agent.memory_store import CATEGORIES, Item
from ironm_agent.memory_view import Recall, until_text
from ironm_agent.personal import Personal
from ironm_agent.roles import GENERAL, Role
from ironm_agent.skills import Skill, by_id, catalog_any, prompt_of
from ironm_agent.tone import BUDDY
from ironm_agent.tools import Toolbox
from ironm_agent.training import weekday_name

# 他说的状况、要照顾到的情况怎么记（§166）：默认回答后系统在后台记；切回作答时当场调工具（§122、§145）
RECORD_AFTER = '他说的身体状况和要照顾到的情况（伤病、器械、作息、偏好），系统在回答后自己记下，回答里不用说"记下了"。'
RECORD_INLINE = "他说了自己今天的状况调用 record_state，说了关于他本人、以后要照顾到的情况（伤病、器械、作息、偏好、近期的事）调用 remember；"
HARD_S = 42.0  # 作答最长多少秒（加上作答前的准备约 45 秒）；到了就停下说没答上来（§165）
LOAD_SKILL = "load_skill"
APP_HELP = "app_help"

LOAD_SKILL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": LOAD_SKILL,
        "description": (
            "装一份规程（开头一张卡：管哪几个决定或做事步骤、判断要点、怎么说），或者取已装规程的某一节细节。"
            "id 从系统提示里的规程目录里选；要细节时带 section（卡片后面列的分节编号或名字，写「全部」取整份）。"
            "算营养、选方案、检查计划、判断疼痛、看趋势这类专业工具跟着规程走：工具列表里没有要用的，先装对应的规程。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "description": "规程编号，如 plan-build"},
                "section": {
                    "type": "string",
                    "description": "要看的那一节：编号（如 5）或名字（如 选初步方案）；不写给开头那张卡",
                },
            },
            "required": ["id"],
        },
    },
}
APP_HELP_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": APP_HELP,
        "description": "查这个 App 的使用说明（哪个页面、点哪里、怎么做）。用户问 App 怎么用时调用。",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
    },
}


def _skill_words(skill: Skill, flags: Collection[str] = ()) -> str:
    """整份规程的文字加它带的工具名：用来看它提到了哪些专业工具（§212）。"""
    return skill.text(flags, full=True) + " " + " ".join(skill.toolset)


async def _load_skill(box: Toolbox, args: Mapping[str, Any]) -> str:
    skill = by_id(str(args.get("id", "")).strip())
    if skill is None:
        return "没有这个规程编号，请从目录里选。"
    box.adopt(e for r in skill.refs_for(box.flags) if (e := box.kb.get(r)) is not None)
    if skill.id not in box.loaded:
        box.loaded.append(skill.id)
    # 这份规程提到的专业工具这时才给（§212）：取一节也算装上了
    more = box.open_pro(box.pro_named(_skill_words(skill, box.flags)))
    which = str(args.get("section") or "").strip()
    if which:  # 取一节细节（§99）
        part = skill.section(which)
        if part is None:
            return f"没有这一节。{skill.name}的分节：{skill.index()}"
        return part + ("\n\n装上后多了这些工具：" + "、".join(more) + "。" if more else "")
    text = skill.text(box.flags)
    # 规程带的动作工具（改计划、新计划、记训练）这时才开放（§120）；装训练类规程时先确认条件的那个也给（§122）
    desk = box.actions
    tools = [*skill.toolset, *([ASK_TERMS] if skill.role == Role.TRAINER else [])]
    opened = [*(desk.unlock(tools) if desk is not None else []), *more]
    if opened:
        text += "\n\n装上后多了这些工具：" + "、".join(opened) + "。"
    held = getattr(
        box, "actions_held", ""
    )  # 计划和记录的做法这一轮没放进系统提示（§96）：装训练类规程时补上
    if held and skill.role == Role.TRAINER:
        box.actions_held = ""
        return text + "\n\n计划和训练记录（只在需要时做）：\n" + held
    return text


async def _app_help(box: Toolbox, args: Mapping[str, Any]) -> str:
    return "使用说明（照着答，说明里没有的功能如实说还没有）：\n" + app_guide()


EXTRA_TOOLS = {LOAD_SKILL: (LOAD_SKILL_SCHEMA, _load_skill), APP_HELP: (APP_HELP_SCHEMA, _app_help)}

# 不变的放前面、每轮不同的（动作写法、回答分段、装上的规程）放最后：提示缓存按开头相同的部分算（§165）
_PROMPT = """你是健身 App 里的「AI 健身管家」：一位持证体能教练，同时懂运动营养和运动康复。训练、饮食、恢复与睡眠、身体数据的解读、App 怎么用、闲聊，都由你一个人回答。

{tone}

{framework}

先后（冲突时按这个来）：安全规则（急症、就医、不诊断不开药）> 回答格式（先答问的、篇幅、最后那几段怎么写）> 规程 > 说话方式（他自己问到的照答）；规程里"输出"一段只是这类回答的结构参考。

先看上下文再答：
1. 先判断这句话和前面的关系：新问题、对上一条回答的追问或质疑、补充条件、对计划预览的意见，还是闲聊。接着上一轮的，就接着上一轮答。被质疑时先核对你上一条回答和预览里实际说过什么：说错或漏了就认错并改正；没说错就用你说过的内容回答，不要顺着认错。
2. 只答问的：第一句就是答案，原因放后面，没问到的不说，等他追问再展开。他让你在几个选项里选一个（"四选一""是哪一种""去急诊还是在家观察"），第一句就说选的是哪个，再说依据和怎么办。做健康判断（化验变化、报告、用药、去不去医院、多急）时照「身体数据与临床」先核对，第一句结论后可以跟 2–4 条关键理由；他给了判断方法、医院规程或医生的标准，按那份判、说依据哪一条。他一句话问了几件事，逐件答，按他问的顺序，每件先给答案。他提了要求（"给我安排个轻松的"），第一句先接住：能做就直接给，不能做再说为什么。他只说自己的状况（"今天有点不舒服""好累"）、没问怎么办时，关心一句、问怎么了，不主动给做法和安排。提起记忆和今天的状态像朋友当面说话：用问候或一句话带过（"前两天说感冒了，好点了吗"），不报日期，不转述系统的判断（"现在还没好"），他没问的计划安排（今天本来是休息日、病后第一次按几成）不说。超出健身、饮食、恢复、身体数据的问题，说明帮不上、能帮什么。不诊断、不开药。
3. 关于这个人的事实（状态、伤病、计划、记录、数据）只照档案、数据、记忆和对话说，不推测、不编造；数据不是最近 7 天的，说清那不是最近的（"上次""之前"），不当成现在的状态；给具体的量（组次、克数、分钟），数字用工具结果、档案或资料里的，不自己估。
4. 需要信息就调用工具（这一轮能用的都在工具列表里），一轮可以调几次，拿到要的就作答；要查几样（几个动作、几条资料）就在同一次里一起调用，不要查一样等一样。问 App 怎么用时调用 app_help；问天气、哪天能在外面练调用 query_weather。要拿主意、按步骤办事时调用 load_skill 装规程（目录在下面）。

出处（系统会逐句核对）：用到资料的句子句末写〔K03〕，用到档案或计算结果写〔C2〕，凭经验写〔经验〕；用到他的健康数据写成 <statement>这句话<cite>[r3]</cite></statement>。带数字的句子都要有出处，数字照资料、计算或数据写。

{record}说了量的体重、腰围这类数调用 log_body，说了吃了什么调用 log_meal；要换目标方向或定目标体重和日期调用 propose_goal（出卡片，他确认才改）；只记他说了的，问话、假设、别人的不记。

规程目录（要拿主意、按步骤办事时，对得上下面哪份就先用 load_skill 装上照着做，可以和查数据、查资料放在同一次调用里一起调；打招呼、闲聊、简单回一句什么都不用装）：
{catalog}

计划和训练记录（只在需要时做；只是问问、抱怨、闲聊、解释时不做，计划不动）：
{actions}

{slots}
打招呼、道谢、闲聊时只写 answer（一两句自然回应），其余几段留空，不报今天的计划、打卡和数据。{skills}"""

_ASK_FIRST = (
    "- 用户明确要一份新计划（还没有计划、要重排），或长期条件变了（换了健身房、以后每周只能练两天）要重来：这一轮不交计划，"
    f"先调用 {ASK_TERMS}，"
    '回答以"排新计划前先确认一下"开头，用你自己的话把"排新计划要用的条件"（目标、场景、器械、时间、伤病）列给他确认：'
    "有旧答案的说出来、问还是不是这样，没有的问一句；最后请他说哪几项变了、是临时的还是以后都这样。"
)
_CONFIRMED = (
    f"- 用户要一份新计划：前几轮你列过排新计划的条件。他已经确认过（或说了哪几项变了、说「你定」）的，按确认的条件调用 {NEW_PLAN}（kind 写 new）交完整的计划；"
    f"他还没回应过那张清单就不算确认：这一轮先不交计划，调用 {ASK_TERMS}，"
    '回答以"排新计划前先确认一下"开头把条件再列给他确认。'
)
_TEMPORARY = (
    "- 临时的变化（这周出差、生病、这几天忙、只能在酒店练）：记成近期事件，回答这几天怎么练；不排新计划。"
    "他要求改计划时，只改受影响的那几天。"
)
_JUST_ASKED = (
    "- 上一轮你列了排新计划要用的条件请他确认。他这句话是确认（如「按这个排」「可以」「你定」）或改了其中几项："
    f"这一轮就按确认的条件调用 {NEW_PLAN}（kind 写 new）交完整的计划（不要只写在回答里）；确认的条件和记忆里不一样的（目标、器械、时间），调用 remember 用 update 改那一条；他换了别的话题，就答他的话题，不交计划。"
)
_EDIT = (
    f"- 用户要改现有计划的某几处（换动作、挪天、改组次、删、减量）：调用 {PLAN_EDIT}，只交要改的地方（写法见工具说明）；"
    f"要在现有计划上换分化、整份重排、几种操作说不清时调用 {NEW_PLAN}（kind 写 rewrite）交整份。"
)
_LOG = (
    f"- 用户在报告自己这次练了什么：调用 {LOG_WORKOUT} 交这次的记录，数字只来自他的话，没说的不填。"
)
_RESULT = (
    "- 这些工具装了排训练计划（plan-build）、记训练与复盘（log-review）才有，没有时先用 load_skill 装上。"
    "调用后系统会告诉你结果：出了预览的，用一两句话说改了什么、为什么（卡片会显示全部内容，不要再写一遍），系统另外换了动作的照结果说；"
    "被退回的，按原因改了再交，或者照原因跟他说没改成、请他说清楚。没有成功调用，就不要说「已经改了」「记下了」。"
)

# 这一轮不涉及训练时，计划和记录的做法不放进系统提示，只留这一句（§96）
ACTIONS_SHORT = "（这一轮不涉及训练；要改计划、排计划或记训练时，先用 load_skill 装 plan-build 或 log-review，工具会一起给你。）"


def actions_prompt(*, has_plan: bool, confirmed: bool, just_asked: bool = False) -> str:
    parts = [_JUST_ASKED] if just_asked else []
    if has_plan:
        parts.append(_EDIT)
    parts.append(_CONFIRMED if confirmed else _ASK_FIRST)
    parts.append(_TEMPORARY)
    parts.append(_LOG)
    parts.append(_RESULT)
    return "\n".join(parts)


def memory_text(known: Mapping[str, Item], today: date) -> str:
    """单 Agent 看到的记忆：带编号（更新、标为已解决时用），近期事件写明哪天说的、到哪天为止。"""
    if not known:
        return f"这个人的记忆：（还没有）。今天是 {today.isoformat()}（{weekday_name(today.isoweekday())}）。"
    lines = []
    for h, x in known.items():
        when = ""
        if x.category in ("event", "advice"):
            when = (
                f"（{x.created.astimezone().month}月{x.created.astimezone().day}日{until_text(x)}）"
            )
        done = "（已解决）" if x.status == "resolved" else ""
        lines.append(f"{h}｜{x.category} {CATEGORIES[x.category]}{done}：{x.content}{when}")
    return (
        f"这个人的记忆（编号｜类别：内容；照顾到，和这次问题无关的不要提；今天是 {today.isoformat()}（{weekday_name(today.isoweekday())}））：\n"
        + "\n".join(lines)
    )


def framework_for() -> str:
    """管家提示词里的专业部分只留各专业要点的总览；领域的判断规则从基础规程来（§74）。"""
    return FRAMEWORKS[Role.GENERAL]


async def respond(
    text: str,
    history: Sequence[str],
    concerns: Sequence[str],
    toolbox: Toolbox,
    llm: LLM,
    personal: Personal | None,
    memory: Recall | None,
    *,
    case: CaseFile,
    skills: Sequence[Skill],
    note: str = "",
    has_plan: bool = False,
    confirmed: bool = False,
    just_asked: bool = False,
    today: date | None = None,
    images: Sequence[Mapping[str, Any]] = (),
    known: Mapping[str, Item] | None = None,
    memory_block: str | None = None,
    training: bool = True,
    record_after: bool = True,
    conversation: Conversation | None = None,
) -> Consultation:
    """单 Agent 作答一轮。出错时返回不发言的意见，不向外抛（和专业角色一样）。
    training：这一轮涉及训练才放计划和记录的写法；不涉及时装上训练类规程再补（§96）。"""
    flags = flags_of([x.key for x in case.risks], bool(case.injuries))
    if (
        toolbox.actions is not None
    ):  # 规程带的动作工具（§120）；状态、读数、一餐每轮都给，涉及训练时先确认条件的那个也给（§122）
        # 记状态、记忆放到回答后（§166）：规程自带的、这一轮再装的规程带的也不给
        toolbox.actions.withheld = frozenset(AFTER) if record_after else frozenset()
        toolbox.actions.unlock(
            [*ALWAYS, *([ASK_TERMS] if training else []), *(t for x in skills for t in x.toolset)]
        )
    actions = actions_prompt(has_plan=has_plan, confirmed=confirmed, just_asked=just_asked)
    # 专业工具只给这一轮装着的规程提到的（§212）：没装规程的轮次（闲聊、记一餐）不带，中途装了再开
    toolbox.pro_open = {n for x in skills for n in toolbox.pro_named(_skill_words(x, flags))}  # 跟着装上的规程（§212）
    toolbox.actions_held = "" if training else actions
    system = _PROMPT.format(
        tone=BUDDY,
        framework=framework_for(),
        record=RECORD_AFTER if record_after else RECORD_INLINE,
        actions=actions if training else ACTIONS_SHORT,
        slots=slots_prompt(text, None),
        skills=prompt_of(skills, flags),
        catalog=catalog_any(),
    )
    toolbox.flags = flags
    # 规程（含基础规程的判断规则）和总览里引用的资料，照抄时也要能通过核对
    toolbox.adopt(
        e for x in skills for r in x.refs_for(flags) if (e := toolbox.kb.get(r)) is not None
    )
    toolbox.adopt(
        e
        for r in (*framework_refs(Role.GENERAL), *flag_refs(flags))
        if (e := toolbox.kb.get(r)) is not None
    )
    asked = _user_message(
        text,
        history,
        concerns,
        personal.text() if personal else "",
        memory_block
        if memory_block is not None
        else memory_text(known, today or date.today())
        if known is not None
        else memory.shared()
        if memory is not None
        else "",
        case=case.text(calcs=True),  # 每轮都带档案（§255）
        note=note,
        # 依据按需查（§255）：不再按字面预取依据库；他点到名字的动作照旧带上动作库条目（名字对得上才带）
        prefetched=toolbox.prefetch(text),
        # 本会话前面的部分（§254）：最近几轮原文、各轮查到和做了的（登记成计算结果能引用）、更早的摘要
        conversation=conversation_text(conversation, toolbox.add_calc) if conversation is not None else "",
    )
    content: str | list[Any] = [{"type": "text", "text": asked}, *images] if images else asked
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]
    said = "\n".join(x for x in (text, memory.numbers_text() if memory else "") if x)
    if personal is not None:
        toolbox.evidence.support.update(personal.support)
        toolbox.evidence.rows.update(personal.rows)
    trace: dict[str, Any] = {"rounds": 0, "checks": [], "fixed": False, "repaired": False,
                             "exhausted": False, "forced": False}  # fmt: skip
    start = time.perf_counter()
    try:  # 一轮最多等 HARD_S 秒：到了就停下说没答上来，不等到超时重试完（§165）
        opinion = await asyncio.wait_for(
            _loop(GENERAL, messages, toolbox, llm, said, today, trace, question=text), HARD_S
        )
    except TimeoutError:
        trace["timeout"] = True
        silent = Opinion(role=Role.GENERAL, speak=False, reason="这次没在时限里答出来")
        trace["ms"] = int((time.perf_counter() - start) * 1000)
        return Consultation(silent, toolbox.evidence, "failed", trace)
    except ModelUnavailable:
        silent = Opinion(role=Role.GENERAL, speak=False, reason="这次没能调用模型")
        trace["ms"] = int((time.perf_counter() - start) * 1000)
        return Consultation(silent, toolbox.evidence, "failed", trace)
    trace["ms"] = int((time.perf_counter() - start) * 1000)
    trace["experience"] = experience_share(opinion.body()) if opinion.speak else 0.0
    return Consultation(opinion, toolbox.evidence, "spoke" if opinion.speak else "silent", trace)
