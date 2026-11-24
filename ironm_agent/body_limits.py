"""身体情况 → 排计划、出饮食的限制（spec [2026-10-23] §269）。

只用会持续的身体情况（后台读化验时写进 states 的，§269；加上化验、用药由代码认出的，body_risks.py）；
等复查的、只偏一点的不改计划和方案，只在当天的回答里照顾（§269 三层）。

- 饮食：要避开的食物类别（高嘌呤、高钠、高糖……），和出饮食方案时他自己说的忌口同一套检查（diet_compose.avoided）；
  肾负担时蛋白质设上限〔K127〕。
- 训练：转成计划检查认的特殊情况（special.py），排计划、调计划时按硬规则拦。
食物类别按名字认（食物库没有嘌呤、钠这些数），列的是常见的高嘌呤、高钠……食物，宁严勿漏；依据见各条。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

# 化验、用药由代码认出的（body_risks 的 key）→ 身体情况的词
_RISK_STATES = {
    "kidney_low": "肾负担", "alt_high": "肝负担", "hypo_drug": "容易低血糖", "anemia_marked": "携氧差",
    "anemia_severe": "携氧差", "hyperthyroid": "代谢偏快", "ecg_alert": "心律问题",
}  # fmt: skip

# 食物类别：名字里有这些说法就算（"无糖、零度"的饮料不算含糖）
FOOD_GROUPS: dict[str, re.Pattern[str]] = {
    "高嘌呤": re.compile(r"猪肝|鸡肝|鸭肝|牛肝|羊肝|肝尖|腰子|腰花|猪肾|猪肚|毛肚|牛肚|羊肚|肥肠|大肠|小肠|猪心|鸡心|鸭心|脑花"
                       r"|鸭血|猪血|毛血旺|内脏|浓汤|骨头汤|老火汤|高汤|火锅|沙丁|凤尾鱼|带鱼|秋刀鱼|鲭鱼|蛤|蚬|扇贝|生蚝"
                       r"|牡蛎|蚌|贻贝|海虹|蛏|鱼干|虾米|虾皮|干贝|啤酒"),  # fmt: skip
    "含糖饮料": re.compile(r"(?<!无糖)(?<!零度)(?:可乐|雪碧|芬达|汽水|奶茶|果汁|甜饮|糖水|蜂蜜水|运动饮料)(?!.*(?:无糖|零度|零糖))"),
    "高糖": re.compile(r"(?<!无糖)(?:可乐|雪碧|芬达|汽水|奶茶|果汁|糖水)(?!.*(?:无糖|零度|零糖))|蛋糕|甜点|冰淇淋|雪糕"
                     r"|甜甜圈|巧克力|饼干|月饼|糖果|蛋挞|圣代"),
    "高钠": re.compile(r"咸菜|榨菜|腌|酱菜|腊肉|腊肠|香肠|火腿|培根|午餐肉|咸鱼|咸蛋|方便面|泡面|泡菜|卤|酱料|豆瓣酱"),
    "高饱和脂肪": re.compile(r"肥肉|五花|肥牛|黄油|奶油|猪油|牛油(?!果)|油炸|炸鸡|炸|酥|培根|肘子|红烧肉|扣肉"),
    "酒": re.compile(r"啤酒|白酒|红酒|黄酒|米酒|鸡尾酒|葡萄酒"),
    "蛋白粉肌酸": re.compile(r"蛋白粉|乳清|肌酸(?!激酶|酐)"),
}

# 身体情况 → 要避开的食物类别（为什么）
DIET_AVOID: dict[str, tuple[tuple[str, str], ...]] = {
    "尿酸高": (("高嘌呤", "尿酸高，少吃高嘌呤的〔K131〕"), ("含糖饮料", "尿酸高，少喝含糖饮料〔K131〕"), ("酒", "尿酸高，少喝酒〔K131〕")),
    "血压高": (("高钠", "血压高，少吃咸的〔经验〕"), ("酒", "血压高，少喝酒〔经验〕")),
    "血糖调控差": (("高糖", "血糖调控差，少吃甜的、少喝含糖饮料〔K121〕"),),
    "血脂偏高": (("高饱和脂肪", "血脂偏高，少吃肥肉、油炸〔经验〕"), ("酒", "血脂偏高，少喝酒〔经验〕")),
    "肾负担": (("蛋白粉肌酸", "肾功能偏低，不加蛋白粉、肌酸〔K127〕"),),
    "肝负担": (("酒", "转氨酶偏高，别喝酒〔K130〕"),),
}  # fmt: skip
KIDNEY_TARGET, KIDNEY_CAP = 0.8, 1.3  # 克/公斤：KDIGO 建议约 0.8，避免超过 1.3〔K127〕

# 身体情况 → 计划检查认的风险 key（special.py 把它们转成特殊情况）
STATE_RISKS: dict[str, tuple[str, str]] = {
    "骨骼弱": ("osteo", "骨量少或骨质疏松：不安排跳跃冲击和负重弯腰扭转，力量照练〔K111〕"),
    "出血风险": ("bleeding", "有出血风险：不安排容易碰撞摔倒的动作和要人保护的极限重量〔经验〕"),
    "不宜憋气发力": ("no_valsalva", "不宜憋气发力：大重量换成每组 8 次以上、能控制呼吸的重量〔K41〕"),
    "心律问题": ("ecg_alert", "心电图或心律有需要医生看的改变：加大强度前先请心内科医生评估〔K14〕"),
    "血压高": ("hypertension", "血压偏高：不憋气、不做大重量〔K41〕"),
    "携氧差": ("anemia_marked", "有贫血：先看医生查原因，高强度和大重量先放一放〔K128〕〔K129〕"),
    "代谢偏快": ("hyperthyroid", "甲功像甲亢：控制住之前不做高强度和大重量〔K129〕"),
    "怀孕": ("pregnancy", "孕期：避开仰卧、跳跃和憋气发力〔K110〕"),
}  # fmt: skip


def states_of(readings: Any, today: date) -> list[str]:
    """会持续的身体情况：后台读出的（states）加上化验、用药由代码认出的。"""
    from ironm_agent.body_risks import hits

    out = list(getattr(readings, "body_states", ()) or ())
    for h in hits(getattr(readings, "labs", ()) or (), getattr(readings, "meds", "") or "",
                  getattr(readings, "body_codes", ()) or (), today):  # fmt: skip
        if h.key in _RISK_STATES:
            out.append(_RISK_STATES[h.key])
    return list(dict.fromkeys(out))


def diet_avoid(states: Iterable[str]) -> list[tuple[str, str]]:
    """要避开的食物类别和原因（同一类只出一次）。"""
    seen: dict[str, str] = {}
    for s in states:
        for group, why in DIET_AVOID.get(s, ()):
            seen.setdefault(group, why)
    return list(seen.items())


def protein_per_kg(states: Iterable[str]) -> tuple[float, float] | None:
    """肾负担时（建议，上限）克/公斤；其余为 None。"""
    return (KIDNEY_TARGET, KIDNEY_CAP) if "肾负担" in set(states) else None


def in_group(text: str, group: str) -> bool:
    pat = FOOD_GROUPS.get(group)
    return bool(pat and pat.search(text))


def groups_in(text: str, groups: Sequence[str]) -> list[str]:
    return [g for g in groups if in_group(text, g)]
