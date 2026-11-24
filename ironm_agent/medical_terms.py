"""查医学资料前把中文换成英文（spec [2026-10-23] §265 需求 7）：mirobody 的离线医学资料只认英文。

化验项目、药名先用 mirobody 自带的名称统一（离线、不读个人数据）换成标准英文名；
它认不出的（心电图、痛风、副作用这类）查下面的小词表；还剩中文的词不发出去。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from functools import lru_cache

_log = logging.getLogger("ironm_agent.medical")
_CJK = re.compile(r"[一-鿿]")
_SPLIT = re.compile(r"[\s,，、;；/和与及]+")

WORDS: dict[str, str] = {
    "心电图": "electrocardiogram", "ST段": "ST segment", "ST段改变": "ST segment changes", "T波": "T wave",
    "早搏": "premature beats", "房颤": "atrial fibrillation", "心动过缓": "bradycardia", "心动过速": "tachycardia",
    "B超": "ultrasound", "超声": "ultrasound", "彩超": "ultrasound", "骨密度": "bone density", "骨质疏松": "osteoporosis",
    "骨量减少": "osteopenia", "脂肪肝": "fatty liver", "胆结石": "gallstones", "肾结石": "kidney stones",
    "甲状腺结节": "thyroid nodule", "痛风": "gout", "高尿酸": "hyperuricemia", "甲亢": "hyperthyroidism",
    "甲减": "hypothyroidism", "贫血": "anemia", "高血压": "high blood pressure", "糖尿病": "diabetes",
    "低血糖": "hypoglycemia", "高血脂": "high cholesterol", "肾病": "kidney disease", "肾功能": "kidney function",
    "肝功能": "liver function", "运动": "exercise", "训练": "exercise", "副作用": "side effects", "饮食": "diet",
    "偏高": "high", "升高": "high", "偏低": "low", "降低": "low", "怀孕": "pregnancy", "补剂": "dietary supplements",
    "肌酸": "creatine", "生物素": "biotin", "西柚": "grapefruit", "相互作用": "interactions",
    # 常见化验：用 MedlinePlus 化验项目页的叫法（比名称统一给的 LOINC 长名更好查）
    "尿酸": "uric acid", "转氨酶": "ALT", "谷丙转氨酶": "ALT", "谷草转氨酶": "AST", "肌酐": "creatinine",
    "尿素": "BUN", "尿素氮": "BUN", "血红蛋白": "hemoglobin", "糖化血红蛋白": "hemoglobin A1c", "血糖": "blood glucose",
    "空腹血糖": "blood glucose", "甘油三酯": "triglycerides", "胆固醇": "cholesterol", "低密度脂蛋白": "LDL cholesterol",
    "高密度脂蛋白": "HDL cholesterol", "促甲状腺激素": "TSH", "游离甲状腺素": "free T4", "肌酸激酶": "creatine kinase",
    "铁蛋白": "ferritin", "维生素D": "vitamin D", "睾酮": "testosterone", "皮质醇": "cortisol", "血钾": "potassium",
    "钾": "potassium", "钠": "sodium", "血常规": "complete blood count", "肾小球滤过率": "eGFR",
    # 常见的药：用通用名（名称统一会把有些药名认成血药浓度的化验）
    "左甲状腺素": "levothyroxine", "优甲乐": "levothyroxine", "二甲双胍": "metformin", "他汀": "statin",
    "阿托伐他汀": "atorvastatin", "瑞舒伐他汀": "rosuvastatin", "美托洛尔": "metoprolol", "比索洛尔": "bisoprolol",
    "氨氯地平": "amlodipine", "硝苯地平": "nifedipine", "缬沙坦": "valsartan", "氯沙坦": "losartan",
    "华法林": "warfarin", "阿司匹林": "aspirin", "胰岛素": "insulin", "格列美脲": "glimepiride", "格列齐特": "gliclazide",
    "别嘌醇": "allopurinol", "非布司他": "febuxostat", "氢氯噻嗪": "hydrochlorothiazide", "呋塞米": "furosemide",
    "泼尼松": "prednisone", "甲巯咪唑": "methimazole",
}  # fmt: skip


@lru_cache(maxsize=512)
def _resolve(word: str) -> str:
    """mirobody 的名称统一：认得出就给标准英文名（去掉方括号和 in Serum 这类后缀）。"""
    try:
        from mirobody.translate.terminology import resolve_indicators

        got = (resolve_indicators([word]).get("results") or [{}])[0]
    except Exception as e:
        _log.warning("terminology unavailable: error_type=%s", type(e).__name__)
        return ""
    if not got.get("resolved"):
        return ""
    name = str(got.get("canonical") or "").split(" [")[0]
    return re.sub(r"\s+in\s+(Serum|Plasma|Blood|Urine).*$", "", name).strip()


def english(query: str) -> str:
    """中文的词换成英文；换不了的中文词去掉；本来就是英文的原样。"""
    if not _CJK.search(query):
        return query.strip()
    out = []
    for w in (x for x in _SPLIT.split(query) if x):
        if not _CJK.search(w):
            out.append(w)
            continue
        got = WORDS.get(w) or _resolve(w)
        if not got:  # 词里带着已知的说法："尿酸偏高" → uric acid high
            hits = [(k, v) for k, v in sorted(WORDS.items(), key=lambda kv: -len(kv[0])) if k in w]
            rest = w
            for k, v in hits:
                rest = rest.replace(k, " ")
                out.append(v)
            for part in (x for x in _SPLIT.split(rest) if x and _CJK.search(x)):
                if r := _resolve(part):
                    out.append(r)
            continue
        out.append(got)
    return " ".join(dict.fromkeys(out)).strip()


def in_english(search: Callable[[str, str], Awaitable[str]]) -> Callable[[str, str], Awaitable[str]]:
    """包一层：查之前先换成英文（名称统一在线程里跑，不卡住）。"""

    async def run(query: str, scope: str = "") -> str:
        q = await asyncio.to_thread(english, query) if _CJK.search(query) else query
        if not q:
            return "这几个词没能换成英文医学词：请换成英文标准名再查（如 uric acid、ALT、metformin）。"
        return await search(q, scope)

    return run
