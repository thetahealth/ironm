"""化验和用药里由代码认出的危险情况（spec [2026-10-23] §265 需求 6）。

只认有明确依据、错了会伤人的几种；认出来的和血压、孕期一样当作风险：档案开头提醒、首页显示，
计划和回答按它核对（constraints.py、special.py）。其余指标对人的影响、对练和吃的影响由模型判断（§265 需求 4）。

- 肾功能明显偏低：eGFR < 60（KDIGO 的 G3 起）→ 蛋白质不往上加、不吃肌酸〔K127〕〔K90〕〔K10〕；
- 在用会引起低血糖的降糖药（胰岛素、磺脲类、格列奈类）→ 别空腹练〔K42〕；
- 血糖很高或很低、钾明显异常、重度贫血 → 先别运动、先看医生〔K129〕；
- 中度贫血、甲亢没控制 → 不排高强度和大重量，先看医生〔K128〕〔K129〕；
- 转氨酶（ALT）是上限的 3 倍以上 → 不推荐补剂〔K130〕；
- 心电图有需要先看医生的改变（后台读报告认出的，见 body_reading.py）→ 加大强度前先请心内科医生评估〔K14〕。

判定只用代码，不调用模型。化验只看还新的：危急的 30 天内、贫血和转氨酶 90 天内、甲功 180 天内、肾功能一年内。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

KIDNEY, HYPO_DRUG, GLUCOSE, POTASSIUM = "kidney_low", "hypo_drug", "glucose_extreme", "potassium_extreme"
ANEMIA_SEVERE, ANEMIA, THYROID, ALT_HIGH, ECG = (
    "anemia_severe", "anemia_marked", "hyperthyroid", "alt_high", "ecg_alert",
)  # fmt: skip
BLOCKING = frozenset({GLUCOSE, POTASSIUM, ANEMIA_SEVERE})  # 先别运动、先看医生（不管问什么都要说一次）
MUST_SAY = BLOCKING | {ECG}  # 不管他问什么，本次对话没说过就要说一次（§265 需求 3）
_ALARM_WORDS = {GLUCOSE: "血糖", POTASSIUM: "钾", ANEMIA_SEVERE: "贫血", ECG: "心电图"}
_DOCTOR = re.compile(r"医生|就医|医院|门诊|急诊|去[^，,。；;]{0,6}科")


def said_alarm(key: str, answers: Sequence[str]) -> bool:
    """本次对话之前的回答里已经说过这件事（提到它、也说了看医生）。"""
    word = _ALARM_WORDS.get(key, "")
    return bool(word) and any(word in x and _DOCTOR.search(x) for x in answers)

EGFR_LOW = 60.0  # mL/min/1.73m²，KDIGO G3 起〔K127〕
GLUCOSE_HIGH, GLUCOSE_LOW = 16.7, 3.0  # mmol/L
POTASSIUM_LOW, POTASSIUM_HIGH = 3.0, 6.0  # mmol/L（经验，落在 K129 说的明显电解质紊乱里）
HB_SEVERE, HB_MARKED = 80.0, 100.0  # g/L：WHO 重度 < 80，中度 80–109〔K128〕；100 是经验
ALT_TIMES = 3.0  # 上限的几倍
FRESH = {GLUCOSE: 30, POTASSIUM: 30, ANEMIA: 90, ALT_HIGH: 90, THYROID: 180, KIDNEY: 365}

# 认指标：化验单上印的名字或标准名（不分大小写）；先排除再认
_EGFR = re.compile(r"egfr|估算肾小球|肾小球滤过率|\bgfr\b", re.I)
_GLUCOSE = re.compile(r"空腹血糖|血糖|葡萄糖|\bglu\b|glucose", re.I)
_NOT_GLUCOSE = re.compile(r"糖化|a1c|尿|urine|耐量|餐后\s*2|2\s*h", re.I)
_HB = re.compile(r"血红蛋白|\bhgb\b|\bhb\b|hemoglobin|haemoglobin", re.I)
_NOT_HB = re.compile(r"糖化|a1c|平均|mch|分布|电泳|electrophoresis", re.I)
_TSH = re.compile(r"\btsh\b|促甲状腺激素|thyroid.?stimulating", re.I)
_FREE_T = re.compile(r"\bft[34]\b|游离甲状腺素|游离三碘|游离\s*t[34]|free\s*t[34]|free thyroxine|free triiodo", re.I)
_NOT_THYROID = re.compile(r"抗体|antibod|受体", re.I)
_ALT = re.compile(r"\balt\b|谷丙|丙氨酸氨基转移酶|\bgpt\b|alanine amino", re.I)
_POTASSIUM = re.compile(r"^\s*(?:血清)?钾(?:离子)?\s*$|血钾|^\s*k\+?\s*$|potassium", re.I)
_NOT_POTASSIUM = re.compile(r"尿|urine", re.I)

# 会引起低血糖的降糖药（胰岛素、磺脲类、格列奈类）〔K42〕；二甲双胍这类单用一般不会，不在这里
HYPO_DRUGS = re.compile(
    r"胰岛素|insulin|甘精|门冬|赖脯|地特|德谷|诺和锐|诺和灵|优泌乐|优泌林|来得时|lantus|humalog|novolog|novorapid"
    r"|levemir|tresiba|toujeo|格列本脲|格列吡嗪|格列齐特|格列美脲|格列喹酮|达美康|亚莫利|美吡达|瑞易宁"
    r"|glibenclamide|glyburide|glipizide|gliclazide|glimepiride|gliquidone|瑞格列奈|那格列奈|米格列奈|诺和龙"
    r"|repaglinide|nateglinide|mitiglinide",
    re.I,
)


@dataclass(frozen=True)
class Hit:
    key: str
    note: str  # 给他看的一句（档案开头、首页）
    block: bool = False


def _num(lab: Any) -> float | None:
    v = getattr(lab, "num", None)
    if v is not None:
        return float(v)
    m = re.match(r"\s*[<>≤≥]?\s*(-?\d+(?:\.\d+)?)", str(getattr(lab, "value", "") or ""))
    return float(m.group(1)) if m else None


def _named(lab: Any, pattern: re.Pattern[str], exclude: re.Pattern[str] | None = None) -> bool:
    names = f"{getattr(lab, 'name', '')} {getattr(lab, 'std', '')}"
    if exclude is not None and exclude.search(names):
        return False
    return any(pattern.search(str(n)) for n in (getattr(lab, "name", ""), getattr(lab, "std", "")) if n)


def _fresh(lab: Any, today: date, days: int) -> bool:
    d = getattr(lab, "day", None)
    return isinstance(d, date) and 0 <= (today - d).days <= days


def _latest(labs: Sequence[Any], pattern: re.Pattern[str], exclude: re.Pattern[str] | None,
            today: date, days: int) -> Any | None:  # fmt: skip
    got = [x for x in labs if _named(x, pattern, exclude) and _num(x) is not None and _fresh(x, today, days)]
    return max(got, key=lambda x: x.day) if got else None


def _unit(lab: Any) -> str:
    return str(getattr(lab, "unit", "") or "").lower().replace(" ", "")


def glucose_mmol(lab: Any) -> float | None:
    v = _num(lab)
    if v is None:
        return None
    return v / 18.0 if "mg/dl" in _unit(lab) else v


def hb_gl(lab: Any) -> float | None:
    v = _num(lab)
    if v is None:
        return None
    unit = _unit(lab)
    if "g/dl" in unit or (not unit and v < 25):  # 13.5 g/dL 这种写法
        return v * 10
    return v


def _when(lab: Any) -> str:
    return f"{lab.day.month} 月 {lab.day.day} 日"


def _over(lab: Any, side: str) -> bool:
    """按化验单印的参考范围（或它标的高低）看是不是超出。"""
    verdict = str(getattr(lab, "verdict", "") or "")
    if verdict:
        return verdict == ("偏高" if side == "high" else "偏低")
    v, low, high = _num(lab), getattr(lab, "low", None), getattr(lab, "high", None)
    if v is None:
        return False
    return (high is not None and v > high) if side == "high" else (low is not None and v < low)


def hits(labs: Sequence[Any], meds: str, codes: Sequence[str], today: date) -> list[Hit]:
    """化验、用药、后台读报告认出的代码 → 危险情况（同一件事只出一条）。"""
    out: list[Hit] = []
    g = _latest(labs, _GLUCOSE, _NOT_GLUCOSE, today, FRESH[GLUCOSE])
    gv = glucose_mmol(g) if g is not None else None
    if gv is not None and (gv >= GLUCOSE_HIGH or gv < GLUCOSE_LOW):
        how = "很高" if gv >= GLUCOSE_HIGH else "很低"
        out.append(Hit(GLUCOSE, f"你 {_when(g)}化验的血糖是 {g.value}{(' ' + g.unit) if g.unit else ''}，{how}：先别运动，尽快去内分泌科看看；"
                               "如果口渴多尿明显、恶心呕吐、心慌出汗手抖或意识不清，马上就医。", block=True))  # fmt: skip
    k = _latest(labs, _POTASSIUM, _NOT_POTASSIUM, today, FRESH[POTASSIUM])
    kv = _num(k) if k is not None else None
    if kv is not None and (kv < POTASSIUM_LOW or kv > POTASSIUM_HIGH):
        how = "偏低得多" if kv < POTASSIUM_LOW else "偏高得多"
        out.append(Hit(POTASSIUM, f"你 {_when(k)}化验的血钾是 {k.value}{(' ' + k.unit) if k.unit else ''}，{how}：先别运动，尽快找医生复查；"
                                  "如果心慌、心跳不齐、肌肉无力，马上就医。", block=True))  # fmt: skip
    hb = _latest(labs, _HB, _NOT_HB, today, FRESH[ANEMIA])
    hv = hb_gl(hb) if hb is not None else None
    if hv is not None and hv < HB_SEVERE:
        out.append(Hit(ANEMIA_SEVERE, f"你 {_when(hb)}化验的血红蛋白是 {hb.value}{(' ' + hb.unit) if hb.unit else ''}，贫血比较重："
                                      "先别运动，尽快去看医生查原因。", block=True))  # fmt: skip
    elif hv is not None and hv < HB_MARKED:
        out.append(Hit(ANEMIA, f"你 {_when(hb)}化验的血红蛋白是 {hb.value}{(' ' + hb.unit) if hb.unit else ''}，有贫血："
                               "先看医生查原因，高强度和大重量先放一放。"))  # fmt: skip
    tsh = _latest(labs, _TSH, _NOT_THYROID, today, FRESH[THYROID])
    free = [x for x in labs if _named(x, _FREE_T, _NOT_THYROID) and _fresh(x, today, FRESH[THYROID])]
    if tsh is not None and _over(tsh, "low") and any(_over(x, "high") for x in free):
        out.append(Hit(THYROID, f"你 {_when(tsh)}的甲功是促甲状腺激素偏低、游离甲状腺激素偏高，像是甲亢："
                                "先去内分泌科看看，控制住之前不做高强度和大重量。"))  # fmt: skip
    egfr = _latest(labs, _EGFR, None, today, FRESH[KIDNEY])
    ev = _num(egfr) if egfr is not None else None
    if ev is not None and ev < EGFR_LOW:
        out.append(Hit(KIDNEY, f"你 {_when(egfr)}化验的 eGFR 是 {egfr.value}，偏低（肾功能下降）：蛋白质别往上加、不吃肌酸，"
                               "吃多少蛋白、补剂能不能吃先问肾内科医生。"))  # fmt: skip
    alt = _latest(labs, _ALT, None, today, FRESH[ALT_HIGH])
    av, ah = (_num(alt), getattr(alt, "high", None)) if alt is not None else (None, None)
    if av is not None and ah and av >= ALT_TIMES * ah:
        out.append(Hit(ALT_HIGH, f"你 {_when(alt)}化验的转氨酶（ALT）是 {alt.value}，是上限的 {av / ah:.0f} 倍左右："
                                 "先找医生看看，补剂、保健品先别吃，也别喝酒。"))  # fmt: skip
    drug = HYPO_DRUGS.search(meds or "")
    if drug:
        out.append(Hit(HYPO_DRUG, f"你在用的{drug.group(0)}可能引起低血糖：别空腹练，练前吃点东西、随身带糖，练完留意头晕手抖出汗。"))  # fmt: skip
    if ECG in codes:
        out.append(Hit(ECG, "你的心电图有需要医生看的改变：加大运动强度前先请心内科医生评估；运动中胸闷胸痛、心慌、头晕立即停下。"))  # fmt: skip
    return out


BRIEFS = {
    KIDNEY: "肾功能偏低（eGFR < 60）：蛋白质不往上加、不吃肌酸，吃多少问肾内科医生",
    HYPO_DRUG: "在用会引起低血糖的降糖药：别空腹练，练前吃点、随身带糖",
    GLUCOSE: "最近化验血糖很高或很低，已提醒尽快就医：不安排任何运动",
    POTASSIUM: "最近化验血钾明显异常，已提醒尽快就医：不安排任何运动",
    ANEMIA_SEVERE: "最近化验贫血比较重，已提醒尽快就医：不安排任何运动",
    ANEMIA: "有贫血：不安排高强度和大重量，先看医生查原因",
    THYROID: "甲功像甲亢：控制住之前不安排高强度和大重量，先看内分泌科",
    ALT_HIGH: "转氨酶是上限的 3 倍以上：不推荐补剂、保健品，别喝酒，先看医生",
    ECG: "心电图有需要医生看的改变：加大强度前先请心内科医生评估",
}
TITLES = {
    KIDNEY: "肾功能偏低：蛋白和补剂先问医生",
    HYPO_DRUG: "降糖药：别空腹练、带糖",
    GLUCOSE: "血糖异常：先别运动，尽快就医",
    POTASSIUM: "血钾异常：先别运动，尽快就医",
    ANEMIA_SEVERE: "贫血较重：先别运动，尽快就医",
    ANEMIA: "贫血：先看医生，强度先放一放",
    THYROID: "甲功异常：先看内分泌科",
    ALT_HIGH: "转氨酶偏高：先别吃补剂",
    ECG: "心电图：加大强度前先问医生",
    "osteo": "骨量少：不做跳跃冲击",  # 后台读出的身体情况（§269，body_limits.STATE_RISKS）
    "bleeding": "出血风险：避开碰撞和极限重量",
    "no_valsalva": "不宜憋气：不做大重量",
}
