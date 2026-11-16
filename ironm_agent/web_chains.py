"""连锁店菜单库的接口（spec [2026-10-23] §203）：图鉴 → 食物 → 连锁店，按品牌、按类列出每份的营养和出处。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ironm_agent import chains
from ironm_agent.web_voice import _verify

router: APIRouter = APIRouter()


@router.get("/iron/api/chains")
async def list_chains(user_id: str = Depends(_verify())) -> dict[str, Any]:
    brands, items = chains.menus()
    out = []
    for key, b in brands.items():
        cats: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            if it.brand == key:
                cats.setdefault(it.cat, []).append(chains.item_json(it))
        out.append({"id": key, "name": b.get("name", key), "source": b.get("source", ""),
                    "official_updated": b.get("official_updated", ""), "fetched": b.get("fetched", ""),
                    "cats": [{"name": k, "items": v} for k, v in cats.items()]})  # fmt: skip
    return {"brands": out}
