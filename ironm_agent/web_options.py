"""推荐下面的按钮（spec [2026-10-23] §261）：最近两天的推荐和选没选；点"记这顿"按推荐时的内容和热量直接记一餐，
不调模型；和对话里写编号记的是同一套（同一份推荐、同一个记录函数、同样标已选）。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ironm_agent.meals import MEAL_KINDS, meal_from, record
from ironm_agent.options import OptionStore, brief, default_options, option_lines

_log = logging.getLogger("ironm_agent.web")
router = APIRouter()


def _verify() -> Any:
    from mirobody.server.auth import verify_token

    return verify_token


def option_store() -> OptionStore:
    return default_options()


def meal_store() -> Any:
    from ironm_agent.web_more import meal_store as meals

    return meals()


OPTIONS = Depends(option_store)  # 测试时用 dependency_overrides 换成内存版
MEALS = Depends(meal_store)


class LogBody(BaseModel):
    kind: str


@router.get("/iron/api/options/recent")
async def recent(user_id: str = Depends(_verify()), store: OptionStore = OPTIONS) -> dict[str, Any]:
    """最近两天还挂着的推荐（按钮显示已记、已过期用）。"""
    return {"options": [brief(o) for o in await store.recent(user_id, date.today())]}


@router.post("/iron/api/options/{oid}/log")
async def log(oid: str, body: LogBody, user_id: str = Depends(_verify()), store: OptionStore = OPTIONS,
              meals: Any = MEALS) -> dict[str, Any]:  # fmt: skip
    """点"记这顿"：按推荐时存下的食物和克数记成选的那一餐，标已选，回一句记了什么。"""
    if body.kind not in MEAL_KINDS:
        raise HTTPException(400, "要选是哪一餐")
    today = date.today()
    got = next((o for o in await store.recent(user_id, today) if o.id == oid.upper() and o.kind == "meal"), None)
    if got is None:
        raise HTTPException(404, "这个推荐已经过期了，在对话里说一声吃了什么就行")
    if got.chosen:  # 别把同一顿记两遍（§264）：又吃了一份就在对话里说
        raise HTTPException(409, "这个已经记过了；又吃了一份就在对话里说一声")
    lines, missed = option_lines([got])
    if not lines:
        raise HTTPException(422, "这个推荐里的东西没对上菜单和食物库，在对话里说一声吃了什么就行")
    meal = meal_from(body.kind, got.label, lines, estimated=True, note="点推荐记的，按菜单和食物库算")  # type: ignore[arg-type]
    _, total = await record(meals, user_id, today, meal)
    await store.choose(user_id, [got.id])
    skipped = f"（{'、'.join(missed)}没对上，没算进去）" if missed else ""
    return {"id": got.id, "chosen": True, "kcal": round(meal.macros.kcal),
            "receipt": f"已记下{body.kind}：{'、'.join(x.name for x in lines)}，约 {round(meal.macros.kcal)} 千卡、"
                       f"蛋白质 {round(meal.macros.protein_g)} 克{skipped}；今天一共约 {round(total)} 千卡。"}  # fmt: skip
