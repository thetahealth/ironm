"""自定义的接口（spec [2026-11-09] §198）：我的食物、我的动作、我的方案，增删改查；方案能存现在的计划、能用这套。

只读写本人的；日志只记种类和数量，不记内容。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ironm_agent.meals import MAX_PHOTO_BYTES
from ironm_agent.mine import (
    FOOD,
    FOOD_PREFIX,
    MOVE,
    PLAN,
    LimitError,
    MyFood,
    MyMove,
    MyPlan,
    check_food,
    check_move,
    check_plan,
    check_room,
    load,
    new_id,
)
from ironm_agent.training import Plan, TrainingStore, plan_content
from ironm_agent.web import _draft_action, _verify, memory_store, training_store
from ironm_agent.web_more import KITCHEN, PAIN, PHOTO_TYPES, PHOTOS, coach_later, plan_change_later

_log = logging.getLogger("ironm_agent.mine")
router: APIRouter = APIRouter()
TRAINING = Depends(training_store)
MEMORY = Depends(memory_store)
MODELS: dict[str, type[MyFood] | type[MyMove] | type[MyPlan]] = {
    FOOD: MyFood,
    MOVE: MyMove,
    PLAN: MyPlan,
}
PATHS = {"foods": FOOD, "moves": MOVE, "plans": PLAN}


def _kind(what: str) -> str:
    if what not in PATHS:
        raise HTTPException(404, "没有这一类")
    return PATHS[what]


def _bad(err: LimitError) -> HTTPException:
    return HTTPException(422, str(err))


def _photo_key(kind: str, item_id: str) -> str:
    """配的那张图在照片表里的编号（§233）：和打卡照片同一张加密的表，前面带种类分开。"""
    return f"mine:{kind}:{item_id}"


async def _with_photo(photos: Any, user_id: str, kind: str, ids: list[str]) -> set[str]:
    """哪几条配了图；照片表读不到时当作都没有，不影响列表。"""
    if photos is None or not ids:
        return set()
    try:
        have = await photos.having(user_id, [_photo_key(kind, i) for i in ids])
    except Exception as e:
        _log.warning("mine photos unavailable: error_type=%s", type(e).__name__)
        return set()
    return {i for i in ids if _photo_key(kind, i) in have}


async def _list(store: Any, user_id: str, kind: str, photos: Any = None) -> dict[str, Any]:
    items = await load(store, user_id, kind)
    pictured = await _with_photo(photos, user_id, kind, [x.id for x in items])
    return {"items": [{**x.shown(), "photo": x.id in pictured} for x in items]}


async def _save(store: Any, user_id: str, kind: str, item: Any, photos: Any = None) -> dict[str, Any]:
    have = await load(store, user_id, kind)
    if item.id and not any(x.id == item.id for x in have):
        raise HTTPException(404, "这一条不存在")
    try:
        check_room(have, kind, item.id)
        if kind == FOOD:
            check_food(item, have)
        elif kind == MOVE:
            check_move(item, have)
        else:
            check_plan(item.plan, await load(store, user_id, MOVE))
    except LimitError as err:
        raise _bad(err) from None
    item = item.model_copy(update={"id": item.id or new_id(FOOD_PREFIX if kind == FOOD else "")})
    await store.put(user_id, kind, item.id, item.model_dump(mode="json"))
    _log.info(
        "mine saved: kind=%s count=%d",
        kind,
        len(have) + (0 if any(x.id == item.id for x in have) else 1),
    )
    listed = await _list(store, user_id, kind, photos)
    mine = next((x for x in listed["items"] if x["id"] == item.id), None)
    return {"item": mine or {**item.shown(), "photo": False}, **listed}


@router.get("/iron/api/mine/{what}")
async def list_mine(
    what: str, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    return await _list(store, user_id, _kind(what), photos)


@router.post("/iron/api/mine/foods")
async def add_food(
    body: MyFood, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    return await _save(store, user_id, FOOD, body.model_copy(update={"id": ""}), photos)


@router.post("/iron/api/mine/moves")
async def add_move(
    body: MyMove, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    return await _save(store, user_id, MOVE, body.model_copy(update={"id": ""}), photos)


@router.post("/iron/api/mine/plans")
async def add_plan(
    body: MyPlan, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    return await _save(store, user_id, PLAN, body.model_copy(update={"id": ""}), photos)


@router.put("/iron/api/mine/foods/{item_id}")
async def put_food(
    item_id: str, body: MyFood, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    return await _save(store, user_id, FOOD, body.model_copy(update={"id": item_id}), photos)


@router.put("/iron/api/mine/moves/{item_id}")
async def put_move(
    item_id: str, body: MyMove, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    return await _save(store, user_id, MOVE, body.model_copy(update={"id": item_id}), photos)


@router.put("/iron/api/mine/plans/{item_id}")
async def put_plan(
    item_id: str, body: MyPlan, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    return await _save(store, user_id, PLAN, body.model_copy(update={"id": item_id}), photos)


@router.delete("/iron/api/mine/{what}/{item_id}")
async def delete_mine(
    what: str, item_id: str, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    kind = _kind(what)
    if not await store.delete(user_id, kind, item_id[:40]):
        raise HTTPException(404, "这一条不存在")
    try:  # 配的图一起删（§233）；删不掉不拦着，条目已经没了
        await photos.drop(user_id, _photo_key(kind, item_id[:40]))
    except Exception as e:
        _log.warning("mine photo not dropped: error_type=%s", type(e).__name__)
    _log.info("mine deleted: kind=%s", kind)
    return await _list(store, user_id, kind, photos)


async def _own(store: Any, user_id: str, what: str, item_id: str) -> tuple[str, str]:
    """这一条是不是他的：不是、不存在一律 404（看不出别人有没有）。"""
    kind = _kind(what)
    item_id = item_id[:40]
    if not any(x.id == item_id for x in await load(store, user_id, kind)):
        raise HTTPException(404, "这一条不存在")
    return kind, item_id


@router.post("/iron/api/mine/{what}/{item_id}/photo")
async def add_mine_photo(
    what: str,
    item_id: str,
    photo: Annotated[UploadFile | None, File()] = None,
    user_id: str = Depends(_verify()),
    store: Any = KITCHEN,
    photos: Any = PHOTOS,
) -> dict[str, Any]:
    """给自己加的一条配一张图（§233）；再传一张就换掉。图不交给模型，日志不记图片。"""
    kind, item_id = await _own(store, user_id, what, item_id)
    if photo is None or not photo.filename:
        raise HTTPException(422, "选一张图")
    mime = photo.content_type or ""
    if mime not in PHOTO_TYPES:
        raise HTTPException(415, "图片只支持 JPG、PNG、WebP")
    data = await photo.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "图片不能超过 5MB")
    await photos.put(user_id, _photo_key(kind, item_id), data, mime)
    _log.info("mine photo added: kind=%s", kind)
    return {"id": item_id, "photo": True}


@router.get("/iron/api/mine/{what}/{item_id}/photo")
async def get_mine_photo(
    what: str, item_id: str, user_id: str = Depends(_verify()), photos: Any = PHOTOS
) -> Response:
    """只按登录的人和编号取，取不到一律 404。"""
    found = await photos.get(user_id, _photo_key(_kind(what), item_id[:40]))
    if found is None:
        raise HTTPException(404, "没有这张图")
    return Response(found[0], media_type=found[1], headers={"Cache-Control": "private, max-age=600"})


@router.delete("/iron/api/mine/{what}/{item_id}/photo")
async def drop_mine_photo(
    what: str, item_id: str, user_id: str = Depends(_verify()), store: Any = KITCHEN, photos: Any = PHOTOS
) -> dict[str, Any]:
    kind, item_id = await _own(store, user_id, what, item_id)
    await photos.drop(user_id, _photo_key(kind, item_id))
    _log.info("mine photo dropped: kind=%s", kind)
    return {"id": item_id, "photo": False}


class FromCurrentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=24)


@router.post("/iron/api/mine/plans/from-current")
async def save_current(
    body: FromCurrentIn,
    user_id: str = Depends(_verify()),
    store: Any = KITCHEN,
    training: TrainingStore = TRAINING,
) -> dict[str, Any]:
    """把现在的训练计划存一份（库外的动作原样带上：那是 IronM 排的，用这套时照旧）。"""
    stored = await training.plan(user_id)
    if stored is None:
        raise HTTPException(404, "现在还没有训练计划")
    plan = Plan.model_validate(stored.plan.model_dump(mode="json"))
    if body.title.strip():
        plan = plan.model_copy(update={"title": " ".join(body.title.split())})
    have = await load(store, user_id, PLAN)
    try:
        check_room(have, PLAN, "")
    except LimitError as err:
        raise _bad(err) from None
    item = MyPlan(id=new_id(), plan=plan)
    await store.put(user_id, PLAN, item.id, item.model_dump(mode="json"))
    _log.info("mine saved from current plan: count=%d", len(have) + 1)
    return {"item": item.shown(), **(await _list(store, user_id, PLAN))}


@router.post("/iron/api/mine/plans/{item_id}/use")
async def use_plan(
    item_id: str,
    user_id: str = Depends(_verify()),
    store: Any = KITCHEN,
    training: TrainingStore = TRAINING,
    memory: Any = MEMORY,
    pain: Any = PAIN,
) -> dict[str, Any]:
    """用这套：换成当前训练计划（和计划页改计划一样走预览、确认，能撤销）；有伤处的动作照样在备注里注明。"""
    found = next((x for x in await load(store, user_id, PLAN) if x.id == item_id), None)
    if found is None:
        raise HTTPException(404, "这个方案不存在")
    from ironm_agent.constraints import Context, annotate_plan
    from ironm_agent.web_plan import _limits

    _, injured, _ = await _limits(user_id, memory, pain)
    plan = annotate_plan(found.plan, Context(injuries=tuple(injured)))
    draft = await training.add_draft(
        user_id, "plan", plan_content(plan, f"换成我的方案「{plan.title}」")
    )
    done = await _draft_action(training, user_id, draft.id, "confirm")
    plan_change_later(user_id, done.get("draft") or {})
    coach_later(user_id, "用我的方案")
    _log.info("mine plan used")
    return {"draft": done["draft"]}
