"""训练中摄像头看动作要用的识别文件（spec [2026-10-23] §218）。

识别在用户自己的手机或电脑上跑，画面不上传；要用的几个文件（识别程序、引擎、模型）由这台服务器提供，
不从外部加载。文件由 scripts/fetch_pose_model.py 下载到 <数据目录>/pose，不放进仓库。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parents[1]
router: APIRouter = APIRouter()

# 只给这几个文件，别的名字一律 404
FILES: dict[str, str] = {
    "vision_bundle.mjs": "text/javascript",
    "vision_wasm_internal.js": "text/javascript",
    "vision_wasm_internal.wasm": "application/wasm",
    "pose_landmarker_lite.task": "application/octet-stream",
}


def pose_dir() -> Path:
    return Path(os.environ.get("IRONM_DATA_DIR") or ROOT / "data") / "pose"


def available() -> bool:
    d = pose_dir()
    return all((d / name).is_file() for name in FILES)


def _verify() -> Any:
    """和 web._verify 一样（mirobody 的登录校验）；不从 web 导入，免得互相导入。"""
    from mirobody.server.auth import verify_token

    return verify_token


@router.get("/iron/api/pose")
async def pose_status(user_id: str = Depends(_verify())) -> dict[str, Any]:
    """装好了识别文件才在训练中显示"摄像头看动作"；告诉页面第一次要加载多大。"""
    if not available():
        return {"available": False, "mb": 0}
    size = sum((pose_dir() / name).stat().st_size for name in FILES)
    return {"available": True, "mb": round(size / 1_000_000, 1)}


@router.get("/iron/pose/{name}")
async def pose_file(name: str) -> FileResponse:
    """识别文件不涉及个人数据，不要登录（识别程序自己去取引擎和模型时带不上登录凭证）。"""
    kind = FILES.get(name)
    path = pose_dir() / name
    if kind is None or not path.is_file():
        raise HTTPException(404)
    # 文件按版本下载、内容不变：浏览器缓存一周，加载过的不再重复下载
    return FileResponse(path, media_type=kind, headers={"Cache-Control": "public, max-age=604800"})
