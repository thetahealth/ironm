"""连锁店菜单库给管家用（spec [2026-10-23] §214）：按名字或类别查官方数据；几样的合计由这里算。

数照官网原样（千卡由千焦换算后取整，§203）。库里没有的如实说没有，不估；有一样没查到就不给合计，
免得把少算的当成总数。只读菜单库，不读这个人的数据。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ironm_agent import chains
from ironm_agent.protools.result import Finding, Result

MAX_LIST = 12  # 一次最多列几样
_SPACE = re.compile(r"[\s　（）()]+")
_COUNT = re.compile(r"^(\d+|[一二两三四五六])\s*(?:个|份|杯|块|包|只|根)?")
_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}
# 常说的叫法 → 官网的名字（先长后短）
_SAY = (("零度可口可乐", "无糖可口可乐"), ("零度可乐", "无糖可口可乐"), ("无糖可乐", "无糖可口可乐"), ("零度", "无糖可口可乐"))


def _norm(text: str) -> str:
    return _SPACE.sub("", text).lower()


def _brands() -> dict[str, str]:
    return {key: chains.brand_name(key) for key in chains.menus()[0]}


def _strip_brand(text: str) -> str:
    for name in _brands().values():
        text = text.replace(_norm(name), "")
    return text


def _key(term: str) -> str:
    """他说的叫法 → 用来对的字：去空白和括号、去品牌名、常说的叫法换成官网的名字。"""
    t = _strip_brand(_norm(term))
    for said, official in _SAY:
        if said in t:
            return t.replace(said, official)
    return t


def exact(term: str) -> tuple[chains.Item, int] | None:
    """整句话正好是某一样的叫法（"5块麦乐鸡"的 5 块是份量，不是 5 份）。"""
    t = _key(term)
    return next(((item, i) for item, i, keys in _index() if t and t in keys), None)


def _index() -> list[tuple[chains.Item, int, set[str]]]:
    """每样、每个份量和它的叫法（带不带品牌名都算）。"""
    out = []
    for item in chains.menus()[1]:
        for i, size in enumerate(item.sizes):
            names = {chains.food_name(item, i), *chains._aliases(item, i), f"{size.size}{item.name}"}
            if len(item.sizes) == 1 or i == item.default():
                names.add(item.name)  # 不说份量：默认份量
            keys = {_norm(n) for n in names}
            out.append((item, i, keys | {_strip_brand(k) for k in keys}))
    return out


def lookup(term: str) -> tuple[chains.Item, int] | None:
    """他说的一样餐品是菜单库里的哪一样、哪个份量；认不出返回 None。"""
    t = _key(term)
    if not t:
        return None
    rows = _index()
    for item, i, keys in rows:
        if t in keys:
            return item, i
    near = [(item, i) for item, i, keys in rows if any(t in k or (len(k) >= 2 and k in t) for k in keys)]
    if not near:
        return None
    near.sort(key=lambda r: (r[1] != r[0].default(), len(r[0].name)))  # 先默认份量，再名字短的
    return near[0]


def _source(item: chains.Item) -> str:
    b = chains.menus()[0].get(item.brand, {})
    return f"{chains.brand_name(item.brand)}官网营养计算器（官网 {b.get('official_updated', '')}更新）"


def _n(x: float) -> str:
    return f"{x:g}"


def _line(item: chains.Item, i: int, count: int = 1) -> Finding:
    s = item.sizes[i]
    name = chains.food_name(item, i) + (f" × {count}" if count > 1 else "")
    each = f"，每份 {round(s.kcal)} 千卡" if count > 1 else ""
    return Finding(
        name,
        f"{round(s.kcal) * count} 千卡、蛋白质 {_n(s.protein * count)} 克、脂肪 {_n(s.fat * count)} 克、"
        f"碳水 {_n(s.carbs * count)} 克（一份 {_n(s.g)} 克{each}）",
        source=_source(item),
    )


def _split(term: str) -> tuple[str, int]:
    """"2 个板烧鸡腿堡" → ("板烧鸡腿堡", 2)；没写几个就是 1。"""
    term = term.strip()
    m = _COUNT.match(term)
    if not m or not term[m.end() :].strip():
        return term, 1
    raw = m.group(1)
    return term[m.end() :].strip(), max(1, min(int(raw) if raw.isdigit() else _CN[raw], 9))


def _total(picked: Sequence[tuple[chains.Item, int, int]]) -> Finding:
    kcal = sum(round(it.sizes[i].kcal) * n for it, i, n in picked)
    p, f, c = (sum(getattr(it.sizes[i], k) * n for it, i, n in picked) for k in ("protein", "fat", "carbs"))
    names = " + ".join(chains.food_name(it, i) + (f" × {n}" if n > 1 else "") for it, i, n in picked)
    return Finding("合计", f"{kcal} 千卡、蛋白质 {_n(p)} 克、脂肪 {_n(f)} 克、碳水 {_n(c)} 克",
                   how=f"{names}，各样的官方数相加", source=_source(picked[0][0]))  # fmt: skip


def _catalog() -> list[str]:
    lines = []
    for key, name in _brands().items():
        cats: dict[str, int] = {}
        for item in chains.menus()[1]:
            if item.brand == key:
                cats[item.cat] = cats.get(item.cat, 0) + 1
        lines.append(f"{name}：" + "、".join(f"{c}（{n} 样）" for c, n in cats.items()))
    return lines


def chain_menu(query: str = "", pick: Sequence[str] = ()) -> Result:
    """pick 给了：每样的官方数和合计；否则按 query 查（名字或类别）；都没给：有哪些品牌和类别。"""
    if pick:
        found: list[tuple[chains.Item, int, int]] = []
        missing: list[str] = []
        for raw in pick:
            name, count = _split(str(raw))
            lead = str(raw).strip()[: len(str(raw).strip()) - len(name)]  # 开头那个"5 块""2 个"
            whole = exact(str(raw)) or exact(name + lead)  # 数字其实是份量（麦乐鸡 5 块）：不当成几份
            if whole is not None:
                count = 1
            got = whole or lookup(name)
            if got is None:
                missing.append(name)
            else:
                found.append((*got, count))
        lines = [_line(it, i, n) for it, i, n in found]
        notes = [f"菜单库里没有「{m}」，没有官方数据：这一样不要报热量和蛋白质，也不要估。" for m in missing]
        if found and not missing:
            lines.append(_total(found))
        elif found:
            notes.append("有没查到的，这次不给合计；换成库里有的再交一次，或只说查到的那几样。")
        return Result(findings=tuple(lines), notes=tuple(notes))
    q = _strip_brand(_norm(query))
    if not q:
        asked = _norm(query)
        known = any(_norm(n) in asked for n in _brands().values())
        head = ("连锁店菜单库里有（数都来自品牌官网）：" if known or not asked
                else f"菜单库里没有「{query.strip()}」的官方数据，不要报数、不要估。现在有：")  # fmt: skip
        return Result.note(head, *_catalog(), "用 query 写类别或餐品名查具体的；定好几样后写进 pick 算合计。")
    hits = [(it, it.default()) for it in chains.menus()[1]
            if q in _norm(it.name) or q in _norm(it.cat) or _norm(it.name) in q]  # fmt: skip
    one = lookup(query)
    if one is not None and one not in hits:
        hits.insert(0, one)
    if not hits:
        return Result.note(f"菜单库里没有「{query.strip()}」的官方数据，不要报数、不要估。现在有：", *_catalog())
    shown = hits[:MAX_LIST]
    notes = []
    sized = [it.name + "（" + "、".join(s.size for s in it.sizes) + "）" for it, _ in shown if len(it.sizes) > 1]
    if sized:
        notes.append("有几种份量的（上面是默认份量，别的份量把份量写在名字前再查，如「大薯条」）：" + "；".join(sized))
    if len(hits) > MAX_LIST:
        notes.append(f"还有 {len(hits) - MAX_LIST} 样没列出，写得具体一点再查。")
    return Result(findings=tuple(_line(it, i) for it, i in shown), notes=tuple(notes))
