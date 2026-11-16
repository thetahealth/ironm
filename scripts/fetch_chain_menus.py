"""取连锁店官网公开的营养数据，写成 ironm_agent/chain_menus.yaml（spec [2026-10-23] §203）。

用法：
  .venv/bin/python scripts/fetch_chain_menus.py           只比较：列出新上、下架、数值变了的，不改库（按月跑一次）
  .venv/bin/python scripts/fetch_chain_menus.py --write   人看过比较结果后，按官网现在的数据重写库

现在只有麦当劳：https://www.mcdonalds.com.cn/nutrition_calculator 页面里内嵌的营养计算器数据。
只收页面上显示的六项（能量千焦、蛋白质、脂肪、碳水、钠、钙）和数据里的一份多少克；页面没显示的数不收。
开心乐园餐里和别的类重复的餐品（同名或数值完全一样）只留一份，记下它也在哪些类里。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ironm_agent" / "chain_menus.yaml"
UA = "iron-m/0.1 (self-hosted fitness app; monthly nutrition data check)"
MCD_URL = "https://www.mcdonalds.com.cn/nutrition_calculator"
# 营养计算器数据里每个份量的 nutrition 数组：页面显示的是这几位（11-09 对照页面核过）
IDX = {"g": 0, "kj": 1, "protein": 2, "fat": 3, "carbs": 4, "sodium": 5, "calcium": 13}
FIELDS = ("g", "kj", "protein", "fat", "carbs", "sodium", "calcium")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def _num(v: Any) -> float | int:
    x = float(v)
    return int(x) if x.is_integer() else x


def _name(title: str) -> str:
    t = " ".join(str(title).split()).replace("(", "（").replace(")", "）")
    return re.sub(r"\s*（", "（", t)


def parse_mcd(html: str) -> tuple[list[dict[str, Any]], str]:
    """页面 → (餐品列表, 官网写的更新时间)。"""
    i = html.index("var raw_data = ") + len("var raw_data = ")
    raw, _ = json.JSONDecoder().raw_decode(html[i:])
    updated = m.group(1) if (m := re.search(r"更新时间为(\d{4}年\d{1,2}月)", html)) else ""
    items: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for c in raw["category_list"]:
        cat = c["ProductCategory"]["title"].strip()
        for pid in c.get("Product") or []:
            p = raw["Product"].get(str(pid))
            if not p:
                continue
            sizes = []
            for tid in p.get("ProductType") or []:
                t = raw["ProductType"].get(str(tid))
                if not t or not t.get("nutrition"):
                    continue
                n = t["nutrition"]
                sizes.append({"size": " ".join(str(t.get("title") or "").split()), **{k: _num(n[i]) for k, i in IDX.items()}})
            if not sizes:
                continue
            name = _name(p["title"])
            vals = [(s["kj"], s["protein"], s["fat"], s["carbs"], s["sodium"], s["calcium"]) for s in sizes]
            same = [seen.get(("v", v)) for v in vals]  # 开心乐园餐里每个份量在别的类里都有（"玉米杯（小）"）：算同一样
            dup = seen.get(("n", name)) or (same[0] if cat == "开心乐园餐" and all(same) and len({id(x) for x in same}) == 1 else None)
            if dup is not None:
                if cat not in dup["also"] and cat != dup["cat"]:
                    dup["also"].append(cat)
                continue
            item = {"id": f"mcd-{pid}", "name": name, "cat": cat, "also": [], "sizes": sizes}
            items.append(item)
            seen[("n", name)] = item
            for v in vals:
                seen.setdefault(("v", v), item)
    return items, updated


def dump(items: list[dict[str, Any]], updated: str, fetched: str) -> str:
    j = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731  JSON 写法也是合法的 YAML
    lines = [
        "# 连锁店菜单库（spec [2026-10-23] §203）：只收品牌官网公开的营养数据，由 scripts/fetch_chain_menus.py 生成，别手改。",
        "# 每个份量：size 份量名，g 一份多少克（官网数据里的重量），kj 能量（千焦），protein / fat / carbs 克，sodium / calcium 毫克。",
        "# 千卡 = 千焦 ÷ 4.184（和官网页面一样）。also：开心乐园餐等别的类里也有同一样东西。",
        'version: "0.1"',
        "brands:",
        "  mcdonalds:",
        "    name: 麦当劳",
        f"    source: {j(MCD_URL)}",
        f"    official_updated: {j(updated)}",
        f"    fetched: {j(fetched)}",
        "items:",
    ]
    for it in items:
        sizes = ", ".join("{" + ", ".join(f"{k}: {j(s[k])}" for k in ("size", *FIELDS)) + "}" for s in it["sizes"])
        also = f", also: {j(it['also'])}" if it["also"] else ""
        lines.append(f"  - {{id: {j(it['id'])}, brand: mcdonalds, name: {j(it['name'])}, cat: {j(it['cat'])}{also}, sizes: [{sizes}]}}")
    return "\n".join(lines) + "\n"


def compare(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[str]:
    o, n = {x["id"]: x for x in old}, {x["id"]: x for x in new}
    out = [f"新上：{n[k]['name']}（{n[k]['cat']}）" for k in n if k not in o]
    out += [f"下架：{o[k]['name']}（{o[k]['cat']}）" for k in o if k not in n]
    for k in n.keys() & o.keys():
        a = {s["size"]: s for s in o[k]["sizes"]}
        b = {s["size"]: s for s in n[k]["sizes"]}
        for size in a.keys() | b.keys():
            if a.get(size) != b.get(size):
                out.append(f"变了：{n[k]['name']} {size}：{a.get(size)} → {b.get(size)}")
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="按官网现在的数据重写库")
    ap.add_argument("--html", help="用本地存的页面（测试用），不联网")
    args = ap.parse_args(argv)
    html = Path(args.html).read_text(encoding="utf-8") if args.html else fetch(MCD_URL)
    items, updated = parse_mcd(html)
    if not items:
        print("官网页面里没取到数据，没改库", file=sys.stderr)
        return 1
    if args.write:
        OUT.write_text(dump(items, updated, date.today().isoformat()), encoding="utf-8")
        print(f"写好了：{len(items)} 样、{sum(len(x['sizes']) for x in items)} 个份量，官网更新时间 {updated}")
        return 0
    from ironm_agent.yamlfile import load_yaml

    old = load_yaml(OUT)["items"] if OUT.exists() else []
    diff = compare(old, items)
    print("\n".join(diff) if diff else f"和官网一样（{len(items)} 样，官网更新时间 {updated}）")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
