"""记录页的接口（spec [2026-10-23] §272）：一段时间的汇总、肌肉图按周看、一次训练的详情。
只读训练记录和计划，规则算，不调用模型。"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ironm_agent import record
from ironm_agent.training import TrainingStore, week_start
from ironm_agent.web_more import MEALS, PHOTOS, TRAINING, _verify, _web

_log = logging.getLogger(__name__)
SINCE = date(2000, 1, 1)  # "全部"、往前翻周、动作从第一次到现在：都要 30 天以前的记录
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

router = APIRouter()


def _monday(week: str, today: date) -> date:
    """week 写那一周里的任意一天，不写是这周；写错了报 422。"""
    if not week:
        return week_start(today)
    if not _DAY.match(week):
        raise HTTPException(422, "week 写成 2026-11-23 这样")
    try:
        return week_start(date.fromisoformat(week))
    except ValueError:
        raise HTTPException(422, "week 写成 2026-11-23 这样") from None


async def _with_photo(photos: Any, user_id: str, ids: list[str]) -> set[str]:
    try:
        return set(await photos.having(user_id, ids)) if ids else set()
    except Exception as e:  # 照片读不到当作都没拍
        _log.warning("record photos unavailable: error_type=%s", type(e).__name__)
        return set()


@router.get("/iron/api/record")
async def get_record(
    range: str = "month",  # 对外的参数名
    user_id: str = Depends(_verify()),
    store: TrainingStore = TRAINING,
    photos: Any = PHOTOS,
) -> dict[str, Any]:
    """这周 / 这个月 / 全部：练了几次、几组、多久、总重量，六个部位各几组和每个动作从多重到多重，最近三次。"""
    if range not in ("week", "month", "all"):
        raise HTTPException(422, "range 写 week、month 或 all")
    rows = await store.workouts(user_id, SINCE)
    plan = await store.plan(user_id)
    out = record.summary(rows, plan.plan if plan else None, record.part_map(), range, date.today())  # type: ignore[arg-type]
    got = await _with_photo(photos, user_id, [x["id"] for x in out["recent"]])
    for x in out["recent"]:
        x["photo"] = x["id"] in got
    return out


@router.get("/iron/api/record/muscles")
async def get_record_muscles(
    week: str = "", user_id: str = Depends(_verify()), store: TrainingStore = TRAINING
) -> dict[str, Any]:
    """肌肉图按周、按天看：那一周七天各练了哪些动作几组。week 写那一周里的任意一天，不写是这周。"""
    today = date.today()
    monday = _monday(week, today)
    rows = await store.workouts(user_id, SINCE)
    plan = await store.plan(user_id)
    return record.week_days(rows, plan.plan if plan else None, record.part_map(), monday, today)


@router.get("/iron/api/record/workout/{log_id}")
async def get_record_workout(
    log_id: str,
    user_id: str = Depends(_verify()),
    store: TrainingStore = TRAINING,
    photos: Any = PHOTOS,
) -> dict[str, Any]:
    """一次训练的每一组；没有这条或那天没练的返回 404。"""
    rows = await store.workouts(user_id, SINCE)
    hit = next((x for x in rows if x.id == log_id), None)
    if hit is None or not hit.log.done:
        raise HTTPException(404, "没有这次训练")
    out = record.detail(hit, rows, record.part_map())
    out["photo"] = log_id in await _with_photo(photos, user_id, [log_id])
    return out


@router.get("/iron/api/record/meals")
async def get_record_meals(
    week: str = "", user_id: str = Depends(_verify()), meals: Any = MEALS
) -> dict[str, Any]:
    """记录页"吃的"：那一周七天各吃了多少、每一顿是什么。目标热量取那一周周日那天的（这周取今天的），和周报对得上。"""
    today = date.today()
    monday = _monday(week, today)
    rows = await meals.since(user_id, SINCE)
    out = record.eat_week(rows, monday, today, None, None)  # 先收好是哪一周，再按那一周取目标
    kcal = protein = None
    try:
        at = min(date.fromisoformat(out["week"]) + timedelta(days=6), today)
        snap = await _web().snapshot_for(user_id, at)
        kcal = next((m.value for m in snap.metrics if m.key == "kcal_target"), None)
        protein = next((m.value for m in snap.metrics if m.key == "protein_target"), None)
    except Exception as e:  # 目标读不到：照样给记录，只是不画目标线
        _log.warning("record meal targets unavailable: error_type=%s", type(e).__name__)
    return record.eat_week(rows, monday, today, kcal, protein) if kcal else out
