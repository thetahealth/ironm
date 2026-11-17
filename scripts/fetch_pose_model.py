"""下载训练中摄像头看动作要用的识别文件（spec [2026-10-23] §218）到本机数据目录。

用法：.venv/bin/python scripts/fetch_pose_model.py
- MediaPipe 姿态识别（Google，Apache-2.0）：识别程序和引擎取 npm 包 @mediapipe/tasks-vision 1.2.2，
  模型取 pose_landmarker_lite 第 1 版（float16）。一共约 20 MB，放在 <数据目录>/pose。
- 已经有了就什么都不做；每个文件下载完核对 SHA-256，对不上就不装。
- 下载慢时可以用 IRONM_POSE_NPM_BASE、IRONM_POSE_MODEL_URL 换地址（内容要和默认的一样，否则核对不过）。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.2.2"
NPM = f"https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{VERSION}"
MODEL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
UA = "iron-m/0.1 (self-hosted fitness app; model download)"
# 文件名 → (地址里 npm 包下的路径；None 表示模型, SHA-256)
FILES: dict[str, tuple[str | None, str]] = {
    "vision_bundle.mjs": ("vision_bundle.mjs", "b65acb59b370cd6561afd9d04c29ed7a87e7ed6386922ac1b263af1f1ad06b7d"),
    "vision_wasm_internal.js": ("wasm/vision_wasm_internal.js", "f2b9c17131ea79716ec6bf07b117a087b2749a6f8a1e2f1f2e77a9382092db5b"),
    "vision_wasm_internal.wasm": ("wasm/vision_wasm_internal.wasm", "f5bd25147ac89d506074293ce2675969b10e827180db95a0d9689c2ed80764ab"),
    "pose_landmarker_lite.task": (None, "59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a"),
}  # fmt: skip


def target() -> Path:
    return Path(os.environ.get("IRONM_DATA_DIR") or ROOT / "data") / "pose"


def _url(sub: str | None) -> str:
    if sub is None:
        return os.environ.get("IRONM_POSE_MODEL_URL") or MODEL
    return f"{os.environ.get('IRONM_POSE_NPM_BASE') or NPM}/{sub}"


def main() -> int:
    out = target()
    if all((out / name).is_file() for name in FILES):
        print(f"识别文件已经在 {out}")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    for name, (sub, sha) in FILES.items():
        if (out / name).is_file():
            continue
        with tempfile.NamedTemporaryFile(dir=out, prefix=".pose-", delete=False) as tmp:
            digest = hashlib.sha256()
            req = urllib.request.Request(_url(sub), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                while chunk := resp.read(1 << 20):
                    digest.update(chunk)
                    tmp.write(chunk)
        part = Path(tmp.name)
        if digest.hexdigest() != sha:
            part.unlink(missing_ok=True)
            print(f"{name} 下载下来核对不上，没装；可以再跑一次", file=sys.stderr)
            return 1
        part.chmod(0o644)  # 临时文件默认只有自己能读
        part.rename(out / name)
    print(f"识别文件装好了：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
