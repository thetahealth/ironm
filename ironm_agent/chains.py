"""连锁店菜单库（spec [2026-10-23] §203）：品牌官网公开的每份营养，接进食物库，查、记一餐都按它算。

数据在 chain_menus.yaml（scripts/fetch_chain_menus.py 生成，别手改）。每个份量变成食物库里的一样：
名字是"品牌 + 餐品（份量）"，一份 = 官网数据里的克数，每 100 克由每份换算，所以按"一份"记时和官网一字不差。
叫法：名字里带品牌特有字样的（巨无霸、麦辣、板烧、新地……）不加品牌也认；"汉堡包""冰美式"这类通用叫法
必须带品牌（"麦当劳汉堡包"），不然别家的汉堡也会被记成麦当劳。和原有食物库的叫法撞了的不加。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from ironm_agent.yamlfile import load_yaml

MENUS_PATH = Path(__file__).with_name("chain_menus.yaml")
KJ_PER_KCAL = 4.184
# 不带品牌也认的：名字里有这些字样（别家没有的叫法）
DISTINCT = {"mcdonalds": ("麦", "巨无霸", "板烧", "新地", "安格斯", "不素之霸", "V翅", "那么大鸡排", "阿芙佳朵")}
SHORT = {"薯条": "薯", "可口可乐": "可乐"}  # "中薯""大可乐"这类常说的
SIZE_WORDS = ("迷你", "小", "中", "大")


@dataclass(frozen=True)
class Size:
    size: str
    g: float
    kj: float
    protein: float
    fat: float
    carbs: float
    sodium: float
    calcium: float

    @property
    def kcal(self) -> float:
        return self.kj / KJ_PER_KCAL

    def json(self) -> dict[str, Any]:
        n = lambda x: int(x) if float(x).is_integer() else x  # noqa: E731  官网写整数的照整数给
        return {"size": self.size, "g": n(self.g), "kj": n(self.kj), "kcal": round(self.kcal), "protein_g": n(self.protein),
                "fat_g": n(self.fat), "carbs_g": n(self.carbs), "sodium_mg": n(self.sodium), "calcium_mg": n(self.calcium)}  # fmt: skip


@dataclass(frozen=True)
class Item:
    id: str
    brand: str
    name: str
    cat: str
    also: tuple[str, ...]
    sizes: tuple[Size, ...]

    def default(self) -> int:
        """同名不同份量时默认哪个：有中份就中份，否则中间那个。"""
        names = [s.size for s in self.sizes]
        return names.index("中") if "中" in names else (len(names) - 1) // 2


@cache
def menus(path: Path = MENUS_PATH) -> tuple[dict[str, dict[str, Any]], tuple[Item, ...]]:
    raw: dict[str, Any] = load_yaml(path) if path.exists() else {"brands": {}, "items": []}  # type: ignore[assignment]
    items = tuple(
        Item(x["id"], x["brand"], x["name"], x["cat"], tuple(x.get("also") or ()),
             tuple(Size(**{k: float(v) if k != "size" else str(v) for k, v in s.items()}) for s in x["sizes"]))  # fmt: skip
        for x in raw.get("items") or []
    )
    return dict(raw.get("brands") or {}), items


def brand_name(brand: str) -> str:
    return str(menus()[0].get(brand, {}).get("name") or brand)


def food_id(item: Item, i: int) -> str:
    return f"{item.id}-{i}"


def food_name(item: Item, i: int) -> str:
    base = f"{brand_name(item.brand)}{item.name}"
    return f"{base}（{item.sizes[i].size}）" if len(item.sizes) > 1 else base


def _aliases(item: Item, i: int) -> list[str]:
    b, s = brand_name(item.brand), item.sizes[i].size
    distinct = any(w in item.name for w in DISTINCT.get(item.brand, ()))
    names = [item.name] if distinct else []
    bare = item.name.split("（")[0]
    if distinct and bare != item.name and item.cat != "开心乐园餐":  # "麦乐鸡"默认正餐的 5 块，不是开心乐园餐的 4 块
        names.append(bare)
    out: list[str] = []
    if len(item.sizes) == 1 or i == item.default():
        out += [f"{b}{n}" for n in [item.name]] + names  # 不说份量时：默认份量
    if len(item.sizes) > 1 and s in SIZE_WORDS:
        out += [f"{b}{s}{item.name}"] + [f"{s}{n}" for n in names]
        if short := SHORT.get(item.name):
            out.append(f"{b}{s}{short}")
    return out


def food_entries(taken: Iterable[str]) -> list[Any]:
    """菜单库 → 食物库条目（和原有叫法撞了的叫法不加）。"""
    from ironm_agent.foods import Food, Per100

    used = set(taken)
    out = []
    for item in menus()[1]:
        for i, sz in enumerate(item.sizes):
            name = food_name(item, i)
            if name in used:
                continue
            used.add(name)
            aka = []
            for a in _aliases(item, i):
                if a not in used and a != name:
                    used.add(a)
                    aka.append(a)
            g = sz.g if sz.g > 0 else 100.0
            k = 100 / g
            out.append(Food(food_id(item, i), name, tuple(aka), brand_name(item.brand), "份", g,
                            per100=Per100(sz.kcal * k, sz.protein * k, sz.carbs * k, sz.fat * k)))  # fmt: skip
    return out


def item_of(fid: str) -> tuple[Item, int] | None:
    for item in menus()[1]:
        for i in range(len(item.sizes)):
            if food_id(item, i) == fid:
                return item, i
    return None


def item_json(item: Item) -> dict[str, Any]:
    b = menus()[0].get(item.brand, {})
    return {
        "id": item.id, "brand": brand_name(item.brand), "name": item.name, "cat": item.cat, "also": list(item.also),
        "sizes": [{**s.json(), "food_id": food_id(item, i), "food_name": food_name(item, i)} for i, s in enumerate(item.sizes)],
        "default": item.default(), "source": b.get("source", ""), "official_updated": b.get("official_updated", ""),
        "fetched": b.get("fetched", ""),
    }  # fmt: skip
