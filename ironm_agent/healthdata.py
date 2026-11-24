"""从 mirobody 读计算指标要用的数据：每天一个值的序列与最新的身高。只读，不写。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ironm_agent.metrics import Point, Readings

_log = logging.getLogger("ironm_agent.healthdata")

WINDOW_DAYS = 180  # 往回读多久；mirobody 按天汇总时每个指标最多返回最近 92 个点

# 同一类数据在不同来源里名字不同：设备或种子数据按天汇总叫 dailySteps，苹果健康导入后叫
# dailyTotalSteps.apple_health_export（点后面是来源）。先查这个人的指标目录，按去掉来源后的名字认出
# 同一类的所有来源，再按日期合并：同一天取数据最新的那个来源（换了设备、导入了新数据时用新的）。
FAMILIES: dict[str, tuple[str, ...]] = {
    "weight": ("bodyMasss", "bodyMass"),
    "resting_hr": ("restingHeartRates", "dailyRestingHeartRates", "dailyAvgRestingHeartRate"),
    "hrv_sdnn": ("hrvSDNN", "dailyAvgHrvSDNN"),
    "hrv_rmssd": ("hrvRMSSD", "dailyAvgHrvRMSSD"),
    "steps": ("dailySteps", "dailyTotalSteps", "steps"),
    "systolic": ("systolicPressures",),
    "diastolic": ("diastolicPressures",),
    # 身体成分与活动（spec [2026-10-23] §26）：mirobody 已有这些指标
    "lean_mass": ("bodyFatFreeWeight",),
    "active_kcal": ("activeCalories", "dailyCaloriesActive"),
    "exercise_min": ("exerciseMinutes",),
    "vo2max": ("vo2Maxs",),
}
# 体脂率：有的来源记成小数（0.185），有的记成百分数（18.5），统一成百分数
# 手动记的"体脂率"会统一到标准编码（LOINC 41982-0），目录里叫 Percentage of body fat Measured
BODY_FAT = ("bodyFatPercentages", "Percentage of body fat Measured")
# 腰围：苹果健康按米记，手动记录按厘米，统一成厘米
WAIST = ("waistCircumferences",)
_TO_CM = {"m": 100.0, "cm": 1.0, "mm": 0.1}
# 睡眠：各家叫法不同，单位也不同（小时、分钟、毫秒），按单位换成小时
SLEEP = (
    "sleepDuration",
    "dailyTotalSleepTime",
    "dailyTotalSleepAnalysis_Asleep(Total)",
    "sleepAnalysis_Asleep(Total)",
    "dailySleepDuration",
    "totalSleepTime",
)
# mirobody 没有按天汇总的（苹果健康导入的心率变异性），从原始读数按天求平均
RAW_DAILY = {"hrv_sdnn": "hrvSDNN", "hrv_rmssd": "hrvRMSSD"}
_TO_HOURS = {"h": 1.0, "hr": 1.0, "hours": 1.0, "min": 1 / 60, "s": 1 / 3600, "ms": 1 / 3_600_000}


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).split()[0])
    except (ValueError, IndexError):
        return None


def _points(envelope: Any, scale: Mapping[str, float] | None = None) -> tuple[Point, ...]:
    out: list[Point] = []
    for row in getattr(envelope, "data", None) or []:
        if not isinstance(row, Mapping):
            continue
        avg, period = _float(row.get("avg")), str(row.get("period") or "")[:10]
        if avg is None or len(period) != 10:
            continue
        factor = 1.0
        if scale is not None:
            factor = scale.get(str(row.get("unit") or "").strip().lower(), 0.0)
            if not factor:
                continue  # 单位认不出就不用，免得把毫秒当小时
        out.append((date.fromisoformat(period), avg * factor))
    return tuple(sorted(out))


async def _series(svc: Any, who: Mapping[str, str], name: str, start: date, end: date,
                  scale: Mapping[str, float] | None = None) -> tuple[Point, ...]:  # fmt: skip
    envelope = await svc.envelope(
        who, indicators=[name], view="day", start=start.isoformat(), end=end.isoformat()
    )
    if getattr(envelope, "status", "ok") == "error":
        raise LookupError(name)
    return _points(envelope, scale)


async def _sleep_series(svc: Any, who: Mapping[str, str], name: str, start: date, end: date) -> tuple[Point, ...]:
    """睡眠按醒来那天算（§225）：mirobody 把晚上 6 点起算的睡眠记在入睡那晚（local_date），手表 App 和
    用户说的"今天睡了几小时"是醒来那天，按它的 display_day 换过来（按天汇总的，窗口 00:00，它原样返回）。
    入睡那晚是查询条件里的日期，所以往前多查一天，换完再去掉超出范围的。"""
    from mirobody.kernel.series import display_day
    from mirobody.translate.local_day import window_for

    window = window_for(name.split(".")[0])
    points = await _series(svc, who, name, start - timedelta(days=1), end, _TO_HOURS)
    return tuple((day, v) for d, v in points if start <= (day := display_day(d, window)) <= end)


async def _height_cm(svc: Any, who: Mapping[str, str]) -> float | None:
    envelope = await svc.envelope(who, indicators=["heights"], view="latest")
    for row in getattr(envelope, "data", None) or []:
        value = _float(row.get("value")) if isinstance(row, Mapping) else None
        if value is None:
            continue
        unit = str(row.get("unit") or "").lower()
        return value * 100 if unit == "m" or value < 3 else value
    return None


def _family(name: str) -> str:
    """去掉来源后缀：dailyTotalSteps.apple_health_export → dailytotalsteps。"""
    return name.split(".")[0].strip().lower()


def candidates(catalog: list[str], known: tuple[str, ...]) -> tuple[str, ...]:
    """这一类数据要读的名字：固定的候选名，加上目录里同一类的其他来源。"""
    want = {k.lower() for k in known}
    found = [n for n in catalog if _family(n) in want]
    return tuple(dict.fromkeys([*known, *found]))


def merge(series: list[tuple[Point, ...]]) -> tuple[Point, ...]:
    """几个来源按日期合并；同一天有好几个来源时，取最后一条记录最新的那个来源（再看记录多少）。"""
    ranked = sorted((s for s in series if s), key=lambda s: (s[-1][0], len(s)), reverse=True)
    by_day: dict[date, float] = {}
    for s in ranked:
        for day, value in s:
            by_day.setdefault(day, value)
    return tuple(sorted(by_day.items()))


async def _catalog(svc: Any, who: Mapping[str, str]) -> list[str]:
    """这个人有哪些指标（只要名字）；读不到时返回空列表，按固定候选名照常读。"""
    try:
        envelope = await svc.envelope(who)
    except Exception as e:
        _log.warning("catalog unavailable: error_type=%s", type(e).__name__)
        return []
    rows = getattr(envelope, "data", None) or []
    return [str(r["name"]) for r in rows if isinstance(r, Mapping) and r.get("name")]


_RAW_SQL = """
SELECT (time AT TIME ZONE 'UTC' AT TIME ZONE COALESCE(NULLIF(timezone, ''), 'UTC'))::date AS day,
       avg(CASE WHEN value ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN value::double precision END) AS avg
  FROM series_data
 WHERE user_id = :user_id AND indicator = :indicator AND time >= :start AND time < :end
 GROUP BY 1
 ORDER BY 1
"""


async def raw_daily(user_id: str, indicator: str, start: date, end: date) -> tuple[Point, ...]:
    """原始读数按本地日期求每天的平均（mirobody 没有为它做按天汇总时用）。"""
    from mirobody.utils.db import execute_query

    rows = await execute_query(
        _RAW_SQL,
        {
            "user_id": user_id,
            "indicator": indicator,
            "start": start,
            "end": end + timedelta(days=1),
        },
        log_sql=False,
    )
    out = [(r["day"], float(r["avg"])) for r in rows or [] if r.get("avg") is not None]
    return tuple(sorted(out))


async def manual_weight(user_id: str, start: date, end: date) -> tuple[Point, ...]:
    """手动记的体重，按本地日期每天取平均（spec §186）。

    手动记的体重存进 mirobody 后，按天汇总的数后台约一小时才算一次；指标只读汇总的数时，新用户填完体重
    最长要等一小时才算得出热量目标。这里直接读记录（和「身体数据」里列出来的同一个查询，改正、删除照
    mirobody 的规矩），同一天和汇总的数一起合并时以这里为准。只认公斤。"""
    from ironm_agent.readings import default_readings

    rows = await default_readings().recent(user_id, "weight", limit=60)
    by_day: dict[date, list[float]] = {}
    for r in rows:
        if str(r.get("unit") or "kg").strip().lower() not in ("kg", "公斤"):
            continue
        value = _num(r.get("value"))
        try:
            day = date.fromisoformat(str(r.get("day"))[:10])
        except ValueError:
            continue
        if value is not None and start <= day <= end:
            by_day.setdefault(day, []).append(value)
    return tuple(sorted((d, sum(v) / len(v)) for d, v in by_day.items()))


def _num(text: Any) -> float | None:
    try:
        return float(str(text).strip())
    except ValueError:
        return None


async def read_all(
    svc: Any,
    user_id: str,
    today: date,
    raw: Callable[[str, str, date, date], Awaitable[tuple[Point, ...]]] = raw_daily,
    manual: Callable[[str, date, date], Awaitable[tuple[Point, ...]]] = manual_weight,
) -> Readings:
    """读全部数据；某一项读不到时记下来，其余照常。"""
    who = {"user_id": user_id}
    start = today - timedelta(days=WINDOW_DAYS)
    catalog = await _catalog(svc, who)
    names = {k: candidates(catalog, known) for k, known in FAMILIES.items()}
    keys = [(k, n) for k, ns in names.items() for n in ns]
    results = await asyncio.gather(
        *(_series(svc, who, n, start, today) for _, n in keys), return_exceptions=True
    )
    found: dict[str, list[tuple[Point, ...]]] = {k: [] for k in names}
    failures: dict[str, int] = {}
    for (key, _), result in zip(keys, results, strict=True):
        if isinstance(result, BaseException):
            _log.warning("series unavailable: key=%s error_type=%s", key, type(result).__name__)
            failures[key] = failures.get(key, 0) + 1
        else:
            found[key].append(result)
    for key, indicator in RAW_DAILY.items():
        try:
            found[key].append(await raw(user_id, indicator, start, today))
        except Exception as e:
            _log.warning("raw series unavailable: key=%s error_type=%s", key, type(e).__name__)
    try:  # 刚手动记的体重：不等后台按天汇总（§186）
        mine = await manual(user_id, start, today)
    except Exception as e:
        _log.warning("manual weight unavailable: error_type=%s", type(e).__name__)
        mine = ()
    errors = [
        k
        for k, n in failures.items()
        if n == len(names[k]) and not any(found[k]) and not (k == "weight" and mine)
    ]
    got = {k: merge(v) for k, v in found.items()}
    if mine:  # 同一天以手动记的为准
        got["weight"] = tuple(sorted({**dict(got["weight"]), **dict(mine)}.items()))
    sleep_names = candidates(catalog, SLEEP)
    sleep_parts = await asyncio.gather(
        *(_sleep_series(svc, who, n, start, today) for n in sleep_names),
        return_exceptions=True,
    )
    sleep = merge([x for x in sleep_parts if not isinstance(x, BaseException)])
    waist_parts = await asyncio.gather(
        *(_series(svc, who, n, start, today, _TO_CM) for n in candidates(catalog, WAIST)),
        return_exceptions=True,
    )
    waist = merge([x for x in waist_parts if not isinstance(x, BaseException)])
    fat_parts = await asyncio.gather(
        *(_series(svc, who, n, start, today) for n in candidates(catalog, BODY_FAT)),
        return_exceptions=True,
    )
    body_fat = tuple(
        (d, v * 100 if v <= 1 else v)
        for d, v in merge([x for x in fat_parts if not isinstance(x, BaseException)])
    )
    try:
        height = await _height_cm(svc, who)
    except Exception as e:
        _log.warning("height unavailable: error_type=%s", type(e).__name__)
        height = None
    return Readings(sleep_hours=sleep, height_cm=height, waist=waist, body_fat=body_fat,
                    errors=tuple(errors), **got)  # fmt: skip


# ---- 化验（spec [2026-10-23] §255 需求 1）---------------------------------------------------------
# 档案每轮带每项化验最近一次的数、日期、和化验单参考范围比的结果；明细和走势由作答时按需查。

_LABS_SQL = """
SELECT DISTINCT ON (o.series_id)
       COALESCE(NULLIF(o.name_text, ''), NULLIF(o.display_zh, ''), o.display) AS name,  -- 化验单上印的名字在前
       o.value_text, o.value_num, o.unit_text, o.ref_text, o.ref_low, o.ref_high, o.flag_text, o.local_date,
       COALESCE(o.display, '') AS std  -- 标准名（英文），认指标用（§265）
  FROM v_observation o
 WHERE o.user_id = :uid AND o.modality = 'lab-report'
 ORDER BY o.series_id, o.local_date DESC, o.elected DESC, o.observed_start DESC, o.id DESC
"""


@dataclass(frozen=True)
class Lab:
    """一项化验最近一次的结果。"""

    name: str
    value: str
    unit: str
    day: date | None
    ref: str = ""
    verdict: str = ""  # 偏高 / 偏低 / 正常；没有参考范围时为空
    std: str = ""  # 标准名（英文），认指标用（§265）
    num: float | None = None
    low: float | None = None
    high: float | None = None


def lab_verdict(value: float | None, low: float | None, high: float | None, flag: str = "") -> str:
    """和化验单的参考范围比；化验单自己标了高低时以它为准。"""
    flag = (flag or "").strip().lower()
    if flag in ("h", "high", "hh", "偏高", "↑"):
        return "偏高"
    if flag in ("l", "low", "ll", "偏低", "↓"):
        return "偏低"
    if value is None or (low is None and high is None):
        return ""
    if high is not None and value > high:
        return "偏高"
    if low is not None and value < low:
        return "偏低"
    return "正常"


def _float(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


async def latest_labs(user_id: str) -> list[Lab]:
    """每项化验最近一次；读不到时返回空列表。"""
    from mirobody.utils.db import execute_query

    rows = await execute_query(_LABS_SQL, {"uid": str(user_id)}, log_sql=False) or []
    out: list[Lab] = []
    for r in rows:
        day = r.get("local_date")
        out.append(Lab(
            str(r.get("name") or "").strip(), str(r.get("value_text") or "").strip(),
            str(r.get("unit_text") or "").strip(), day if isinstance(day, date) else None,
            str(r.get("ref_text") or "").strip(),
            lab_verdict(_float(r.get("value_num")), _float(r.get("ref_low")), _float(r.get("ref_high")),
                        str(r.get("flag_text") or "")),
            str(r.get("std") or "").strip(), _float(r.get("value_num")), _float(r.get("ref_low")),
            _float(r.get("ref_high")),
        ))  # fmt: skip
    return [x for x in out if x.name and x.value]


def labs_line(labs: Sequence[Lab], today: date) -> str:
    """档案里的一行：偏高偏低的在前；一年以前的写"很久以前"。"""
    order = {"偏高": 0, "偏低": 0, "": 1, "正常": 2}
    parts = []
    for x in sorted(labs, key=lambda x: (order.get(x.verdict, 1), x.name)):
        when = x.day.isoformat() if x.day else "日期不明"
        if x.day and (today - x.day).days > 365:
            when += "，一年多以前"
        judged = f"{x.verdict}，" if x.verdict else ""
        ref = f"参考 {x.ref}" if x.ref else "化验单没印参考范围：不说偏高偏低和正不正常"
        parts.append(f"{x.name} {x.value}{(' ' + x.unit) if x.unit else ''}（{judged}{ref}；{when}）")
    return "；".join(parts)


async def current_meds(user_id: str) -> str:
    """他记在 mirobody 里正在用的药（一段表格文字，认药名用，§265）；读不到时为空。"""
    from mirobody.agent.tools.medications_service import MedicationsService

    out = await MedicationsService().query_medications({"user_id": str(user_id)}, view="plan")
    return str(out.get("result") or "") if isinstance(out, dict) else ""
