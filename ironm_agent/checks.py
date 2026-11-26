"""逐句核对出处：专业 Agent 的意见和管家整合后的回答用同一套规则。

- 引用资料的句子在句末写〔K03〕：句子里的数字只能来自这一句引用的资料；
- 引用指标计算结果的句子在句末写〔C2〕：数字只能来自这个计算结果；
- 引用健康数据的句子写成 <statement>…<cite>[r3]</cite></statement>：数字只能来自引用的数据行；
- 没有资料出处、凭专业经验说的句子，在句末写〔经验〕，不核对数字，用户看得到这是经验；
- 没标任何出处的句子不能有数字（用户自己说的数字除外）。

核对不过、改了一次还不过的，不再整条作废，而是 repair：引用个人数据却对不上的那句删掉（不能把人的数据说错），
其余对不上出处的句子去掉错误的出处、改标〔经验〕。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

from ironm_agent.kb import Entry
from ironm_agent.numbers import traced, written_numbers

EXPERIENCE = "〔经验〕"
KB_REF = re.compile(r"〔(K\d{2,4})〕")
CALC_REF = re.compile(r"〔(C\d{1,3})〕")
ANY_REF = re.compile(r"〔(?:K\d{2,4}|C\d{1,3}|经验)〕")
_STATEMENT = re.compile(r"<statement>.*?</statement>", re.S)
# 句子的结尾：句号、问号、叹号、分号、换行；紧跟在结尾后面的出处标记仍属于前一句
_SENTENCE = re.compile(r"[^。！？；\n]*(?:[。！？；\n]|$)(?:[ \t]*〔(?:K\d{2,4}|C\d{1,3}|经验)〕)*")
_END = re.compile(r"([。！？；]?)(\s*)$")


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.findall(text) if s.strip()]


def _shown(values: Sequence[float]) -> str:
    return "、".join(f"{v:g}" for v in dict.fromkeys(values))


_ENERGY = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[–\-~～至到]\s*(\d+(?:\.\d+)?)\s*)?(?:千卡|大卡|kcal|卡路里)", re.I)


def _energy(text: str) -> list[float]:
    """这句里写的热量数（千卡、大卡），范围两头都算。"""
    return [float(x) for m in _ENERGY.finditer(ANY_REF.sub("", text)) for x in m.groups() if x]


def _derived(loose: Sequence[float], known: Sequence[float]) -> list[float]:
    """对不上的数里，能由这句里已经对上的数算出来的（两个数之差、之和，或全部相加）不算对不上（§256）。"""
    if len(known) < 2:
        return list(loose)
    total = sum(known)
    return [v for v in loose if not (traced(v, [total]) or traced(v, [], data=known))]


def _calc_numbers(text: str, calcs: Mapping[str, str]) -> list[float]:
    return [v for c in CALC_REF.findall(text) if c in calcs for v in written_numbers(calcs[c])]


def _statement_problems(seg_text: str, cites: Sequence[str], *, support: Mapping[str, Sequence[float]],
                        entries: Mapping[str, Entry], calcs: Mapping[str, str], own: list[float]) -> list[str]:  # fmt: skip
    bad_rows = [c for c in cites if c not in support]
    if bad_rows:
        where = "" if support else "（这一轮没有数据行：他的数据在档案和计算结果里，去掉 <statement> 标记，句末标〔C编号〕）"
        return [f"引用了不存在的数据编号：{'、'.join(bad_rows)}{where}"]
    refs = [r for r in KB_REF.findall(seg_text) if r in entries]
    data = [v for c in cites for v in support[c]] + _calc_numbers(seg_text, calcs)
    coefs = [v for r in refs for v in written_numbers(entries[r].as_evidence())]
    loose = [
        v
        for v in written_numbers(ANY_REF.sub("", seg_text))
        if not traced(v, own, data=data, coefs=coefs)
    ]
    return [f"带数据编号的句子里，这些数字在所引的数据行里找不到：{_shown(loose)}"] if loose else []


def _sentence_problems(
    sentence: str,
    *,
    entries: Mapping[str, Entry],
    calcs: Mapping[str, str],
    own: list[float],
    experience: Sequence[float] | None = None,
    rows: bool = True,
) -> list[str]:
    if EXPERIENCE in sentence:
        # 专业 Agent 凭经验说的：不核对数字（experience 为 None），热量除外——热量要照菜单、食物库或计算（§256）；
        # 管家整合时：经验句里的数字必须来自某位专业 Agent 的意见，整合不能自己加数字
        if experience is None:  # 明说是估的（看照片估）照旧算经验；这一轮查到、算出的热量里有这个数的也算对得上
            given = [*own, *(v for c in calcs.values() for v in written_numbers(c)),
                     *(v for e in entries.values() for v in written_numbers(e.as_evidence()))]  # fmt: skip
            kcal = [] if "估" in sentence else [v for v in _energy(sentence) if not traced(v, given)]
            return ([f"「{sentence.strip()[:30]}…」里的热量 {_shown(kcal)} 千卡没有出处：热量照菜单、食物库或计算写，"
                     "句末标对应的〔C编号〕；查不到就不报数"] if kcal else [])  # fmt: skip
        loose = [
            v
            for v in written_numbers(ANY_REF.sub("", sentence))
            if not traced(v, [*experience, *own])
        ]
        return (
            [f"「{sentence.strip()[:30]}…」里的数字 {_shown(loose)} 不在任何专业角色的意见里"]
            if loose
            else []
        )
    refs = [r for r in KB_REF.findall(sentence) if r in entries]
    pool = [v for r in refs for v in written_numbers(entries[r].as_evidence())] + own
    pool += _calc_numbers(sentence, calcs)
    written = written_numbers(ANY_REF.sub("", sentence))
    loose = _derived([v for v in written if not traced(v, pool)], [v for v in written if traced(v, pool)])
    if not loose:
        return []
    where = (
        f"所引的{'、'.join(refs)}"
        if refs
        else "这一句（没有标出处：" + ("用到用户数据要写成 <statement>这句话<cite>[r编号]</cite></statement>，" if rows
                               else "他本人的数据在档案和计算结果里，标〔C编号〕；") +  # 没有数据行不教 r 编号（§274）
        "用到资料要在句末写〔K编号〕，用到指标计算结果写〔C编号〕，凭专业经验说的写〔经验〕）"
    )
    return [f"「{sentence.strip()[:30]}…」里的数字 {_shown(loose)} 在{where}里找不到"]


def check_text(
    text: str,
    *,
    support: Mapping[str, Sequence[float]],
    entries: Mapping[str, Entry],
    question: str,
    calcs: Mapping[str, str] | None = None,
    experience: Sequence[float] | None = None,
) -> list[str]:
    """返回问题清单（为空表示通过）。calcs：指标计算结果（C 编号 → 含数字的说明）；
    experience：整合时经验句允许的数字（各专业 Agent 意见里的数字），专业 Agent 自己写意见时为 None。"""
    from mirobody.kernel.citations import parse

    calcs = calcs or {}
    problems: list[str] = []
    if text.count("<statement>") != text.count("</statement>"):
        problems.append("<statement> 标记没有成对")
    unknown = [r for r in dict.fromkeys(KB_REF.findall(text)) if r not in entries]
    unknown += [r for r in dict.fromkeys(CALC_REF.findall(text)) if r not in calcs]
    if unknown:
        problems.append(f"引用了不存在的资料或计算编号：{'、'.join(unknown)}")
    own = written_numbers(question)
    for seg in parse(text):
        if seg.statement:
            problems += _statement_problems(
                seg.text, seg.cites, support=support, entries=entries, calcs=calcs, own=own
            )
            continue
        for sentence in sentences(seg.text):
            problems += _sentence_problems(
                sentence, entries=entries, calcs=calcs, own=own, experience=experience, rows=bool(support)
            )
    for sentence in sentences(_STATEMENT_TAG.sub("", _CITE.sub("", text))):
        wrong = _false_zero(sentence)
        if wrong:
            problems.append(
                f"「{sentence.strip()[:30]}」说可信范围跨过零，但 {wrong} 并不跨零：范围同号就说在上升或在下降"
            )
    return problems


# "可信范围 a 至 b……跨过零"：a、b 同号时说错了（§47）
_RANGE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:至|到|～|~|–|—)\s*(-?\d+(?:\.\d+)?)")
_CROSS = re.compile(r"跨过\s*(?:零|0)")
_CROSS_CLAUSE = re.compile(r"[，,]?[^，,。！？；\n〔<]*跨过\s*(?:零|0)[^，,。！？；\n〔<]*")


def _false_zero(sentence: str) -> str | None:
    """这句说可信范围跨过零，但写的范围不跨零：返回那个范围。"""
    if not _CROSS.search(sentence):
        return None
    ranges = [(float(a), float(b)) for a, b in _RANGE.findall(sentence)]
    if not ranges or any(a * b <= 0 for a, b in ranges):
        return None
    a, b = ranges[0]
    return f"{a:g} 至 {b:g}"


def _drop_false_zero(text: str) -> str:
    """删掉说"跨过零"却和范围不符的分句，其余保留。"""

    def fix(m: re.Match[str]) -> str:
        sentence = m.group(0)
        if _false_zero(_STATEMENT_TAG.sub("", _CITE.sub("", sentence))) is None:
            return sentence
        return _CROSS_CLAUSE.sub("", sentence)

    return _SENTENCE.sub(fix, text)


def _label_experience(sentence: str) -> str:
    """去掉这句话里的出处标记，句末改标〔经验〕。"""
    bare = ANY_REF.sub("", sentence)
    m = _END.search(bare)
    if m is None:
        return bare + EXPERIENCE
    return bare[: m.start()] + m.group(1) + EXPERIENCE + m.group(2)


def repair(
    text: str,
    *,
    support: Mapping[str, Sequence[float]],
    entries: Mapping[str, Entry],
    question: str,
    calcs: Mapping[str, str] | None = None,
    experience: Sequence[float] | None = None,
) -> str:
    """改不对的地方：对不上的个人数据句删掉；其余对不上出处的句子改标〔经验〕
    （整合时，改标后数字仍不在任何专业角色意见里的句子删掉）。结果一定通过 check_text。"""
    from mirobody.kernel.citations import parse

    calcs = calcs or {}
    own = written_numbers(question)
    text = _drop_false_zero(text)
    kept: list[str] = []

    def hold(m: re.Match[str]) -> str:
        whole = m.group(0)
        seg = next((s for s in parse(whole) if s.statement), None)
        if seg is None:
            return ""
        if _statement_problems(seg.text, seg.cites, support=support, entries=entries, calcs=calcs, own=own):
            # 个人数据说不准就不说：只删对不上的那一小段，同一句里的判断留下（§273）
            cite = "".join(f"[{c}]" for c in seg.cites)

            def fits(part: str) -> bool:
                return not _statement_problems(part, seg.cites, support=support, entries=entries, calcs=calcs, own=own)

            left = trim(seg.text, fits)
            if left is None:
                return ""
            whole = f"<statement>{left}<cite>{cite}</cite></statement>"
        kept.append(whole)
        return _tag(len(kept) - 1)

    body = _STATEMENT.sub(hold, text)
    body = re.sub(r"</?statement>|<cite>.*?</cite>", "", body)  # 不成对的残余标记
    out = []
    for sentence in _SENTENCE.findall(body):
        if not sentence.strip():
            out.append(sentence)  # 换行原样留着，分段的回答修完还是分段（§42）
            continue
        unknown = [r for r in KB_REF.findall(sentence) if r not in entries]
        unknown += [r for r in CALC_REF.findall(sentence) if r not in calcs]
        if unknown or _sentence_problems(
            sentence, entries=entries, calcs=calcs, own=own, experience=experience
        ):
            labeled = _label_experience(sentence)
            if _sentence_problems(
                labeled, entries=entries, calcs=calcs, own=own, experience=experience
            ):
                # 改标经验后还对不上（没出处的热量、整合时加出来的数）：只删那一小段，判断留下（§273）
                # 留下的小段不带数（除了他自己说的）：删掉主语后剩下的数多半是同一件凭印象写的事
                left = trim(labeled, lambda part: not [v for v in written_numbers(ANY_REF.sub("", part))
                                                       if not traced(v, own)] and not _sentence_problems(
                    part, entries=entries, calcs=calcs, own=own, experience=experience))
                if left is not None:
                    out.append(left)
                continue
            out.append(labeled)
        else:
            out.append(sentence)
    repaired = "".join(out)
    for i, whole in enumerate(kept):
        repaired = repaired.replace(_tag(i), whole, 1)
    return repaired.strip()


_CLAUSE = re.compile(r"[^，,、：:]*[，,、：:]?")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_MIN_LEFT = 4  # 删掉对不上的小段后，剩下至少几个汉字才留（§273）


def trim(sentence: str, ok: Callable[[str], bool]) -> str | None:
    """一句里对不上的数所在的那一小段（逗号、顿号、冒号隔开的）删掉，其余留下（§273）。
    ok(句子) 判断一段是否通过核对；出处标记、句末标点照留。删不出能用的（没删掉什么、剩下太短、仍不通过）返回 None。"""
    marks = "".join(ANY_REF.findall(sentence))
    body = ANY_REF.sub("", sentence)
    m = re.search(r"[。！？；\n\s]*$", body)
    end = m.group(0) if m else ""
    core = body[: len(body) - len(end)]
    parts = [p for p in _CLAUSE.findall(core) if p]
    tail = end.strip() or "。"
    kept = [p for p in parts if ok(p.rstrip("，,、：: ") + tail + marks)]
    if len(kept) == len(parts) or not kept:
        return None
    left = "".join(kept).rstrip("，,、：: ")
    if len(_CJK.findall(left)) < _MIN_LEFT:
        return None
    out = left + tail + marks + end[len(end.rstrip()) :]
    return out if ok(out) else None


# 数字后面跟的单位：补出处时要求资料里同一个数后面也是这个单位，免得"3 组"对上资料里不相干的 3（审查 11-05）
_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:[–\-~～至到]\s*(\d+(?:\.\d+)?)\s*)?([%％℃°]|[μµ]?[a-zA-Z]{1,4}|[\u4e00-\u9fff]{1,2})?"
)
# 数后面跟的这些才算单位（§274）：别的字（"520 比参考"的"比参"）不算，那个数按没单位对
UNIT_WORDS = frozenset(
    "千卡 大卡 卡 克 毫克 千克 公斤 斤 次 组 分钟 分 秒 小时 天 周 个月 月 年 岁 厘米 米 公里 步 毫升 升 杯 份 个 块 片 勺 碗 "
    "倍 度 次/分 毫米 毫摩 微摩 单位 粒 支 瓶 盒 层 节 圈 轮 下 口".split()
)


def _unit_of(raw: str) -> str:
    if not raw:
        return ""
    if raw in "%％℃°" or raw[0] in "μµ" or raw[0].isascii():
        return raw.lower()
    if raw in UNIT_WORDS:
        return raw
    return raw[0] if raw[0] in UNIT_WORDS else ""
_ABOUT_HIM = re.compile(r"你|您")


def _units(text: str) -> set[tuple[float, str]]:
    """数和它的单位；后面不是单位的记成空字符串（§274）。"""
    out: set[tuple[float, str]] = set()
    for a, b, u in _UNIT.findall(text):  # 范围"3–4 周"：两头都算带这个单位
        out |= {(float(x), _unit_of(u)) for x in (a, b) if x}
    return out


def _bigrams(text: str) -> set[str]:
    t = "".join(_CJK.findall(text))
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _closest(sentence: str, ids: Sequence[str], calcs: Mapping[str, str]) -> str | None:
    """几份计算结果都有这个数时挑哪份（§274）：和这句用词最接近的，再看内容最短（只说这件事的）。
    热量几份分不出远近时不猜（"目标 2225"和"上周吃了 2225"是两回事），返回 None 照旧退回。"""
    if len(ids) == 1:
        return ids[0]
    near = {c: len(_bigrams(sentence) & _bigrams(calcs[c])) for c in ids}
    top = max(near.values())
    tied = [c for c in ids if near[c] == top]
    if len(tied) > 1 and _energy(sentence):
        return None
    return min(tied, key=lambda c: len(calcs[c]))


def _distinct(v: float) -> bool:
    """没带单位的数，够特别才去出处里找（三位数以上或带小数）：小整数凑巧对上的太多（§274）。"""
    return abs(v) >= 100 or v != int(v)


def _matches(w: tuple[float, str], text: str) -> bool:
    """出处里有这个数：带单位的要单位对得上；没单位的要够特别、出处里写着这个数。"""
    v, u = w
    if u:
        return any(abs(v - hv) < 1e-9 and u[:1] == hu[:1] for hv, hu in _units(text))
    return _distinct(v) and any(abs(v - x) < 1e-9 for x in written_numbers(text))


def _source_for(sentence: str, loose: Sequence[float], *, cited: Sequence[str], entries: Mapping[str, Entry],
                calcs: Mapping[str, str], support: Mapping[str, Sequence[float]]) -> str | None:  # fmt: skip
    """这几个数字全都能对上、又不会认错的那一处出处（§165）：回答别处引过的资料优先；
    其次正好一个计算结果；再次这一轮查到的资料里正好一条；最后健康数据里正好一行。都不行返回 None。
    资料和计算要数字连同单位都对得上（"5 克"对"5 克"）；健康数据只补说他自己的句子（有"你"）。"""
    want = {(v, u) for v, u in _units(sentence) if any(abs(v - x) < 1e-9 for x in loose)}
    if {v for v, _ in want} != {float(x) for x in loose}:
        return None  # 有的数没带单位：说不准是哪来的

    def same(text: str) -> bool:
        return all(_matches(w, text) for w in want)

    def kb(ids: Sequence[str]) -> list[str]:
        return [r for r in ids if r in entries and same(entries[r].as_evidence())]

    if hit := kb(cited):
        return f"〔{hit[0]}〕"
    if hit := [c for c in calcs if same(calcs[c])]:  # 几份都有：标最贴近的那份（§274）
        best = _closest(sentence, hit, calcs)
        return f"〔{best}〕" if best else None
    if len(hit := kb([r for r in entries if r not in cited])) == 1:
        return f"〔{hit[0]}〕"
    if _ABOUT_HIM.search(sentence) and len(
        hit := [r for r, vals in support.items() if all(traced(v, list(vals)) for v in loose)]
    ) == 1:
        return f"<cite>[{hit[0]}]</cite>"
    return _covered(want, cited=cited, entries=entries, calcs=calcs)


def _covered(want: set[tuple[float, str]], *, cited: Sequence[str], entries: Mapping[str, Entry],
             calcs: Mapping[str, str]) -> str | None:  # fmt: skip
    """一句里的数分别来自几处（§256）：每个数连同单位在计算或资料里各找一处（最多三处），
    剩下的能由已对上的数相加减算出来也算。都对不上返回 None。"""
    marks: list[str] = []
    left = set(want)
    order = sorted(calcs, key=lambda c: len(calcs[c]))  # 分处找出处：短的（只说这件事的）在前
    sure = [*((f"〔{r}〕", entries[r].as_evidence()) for r in cited if r in entries),
            *((f"〔{c}〕", calcs[c]) for c in order)]  # fmt: skip
    other = [(f"〔{r}〕", entries[r].as_evidence()) for r in entries if r not in cited]
    pools = [*sure, *other]

    def has(text: str, w: tuple[float, str]) -> bool:
        return _matches(w, text)

    # 资料几条都对得上、计算结果和已引的资料里又没有：可能认错，不猜（审查 11-05）；计算结果几份都有标最短的（§274）
    if any(not any(has(t, w) for _, t in sure) and sum(has(t, w) for _, t in other) > 1 for w in want):
        return None
    if any(w[1][:1] in ("千", "大", "k") and sum(has(calcs[c], w) for c in calcs) > 1 for w in want):
        return None  # 热量几份计算结果都有：可能是两回事（目标和吃了的），不猜（§274）
    for mark, text in pools:
        got = {w for w in left if has(text, w)}
        if got:
            marks.append(mark)
            left -= got
        if not left:
            break
    if left:
        known = [v for v, _ in want - left]
        if _derived([v for v, _ in left], known):
            return None
    return "".join(marks) if marks and len(marks) <= 3 else None


_LEAD = re.compile(r"\s*(?:[-•*]|\d+[.、)])?\s*")


def autocite(
    text: str,
    *,
    support: Mapping[str, Sequence[float]],
    entries: Mapping[str, Entry],
    question: str,
    calcs: Mapping[str, str] | None = None,
) -> str:
    """出处由代码来标（§165、§274），省一次退回模型：
    - 没标或标的出处里没有这个数：数（连同单位）在这一轮的资料、计算、健康数据里找得到就标上；计算结果几份都有标最贴近的；
    - 写了不存在的编号的去掉；写成他本人数据却引了不存在的数据行的，拆掉标记再按上面补；
    - 哪都找不到、不是说他本人（没有"你"）、也不是热量的：标〔经验〕（退回改不好时代码最后也这么标）。
    说他本人的数、热量对不上的不动，照旧退回模型。"""
    from mirobody.kernel.citations import parse

    calcs = calcs or {}
    own = written_numbers(question)
    cited = [r for r in dict.fromkeys(KB_REF.findall(text)) if r in entries]
    changed = False

    def known(ref: str) -> bool:
        return ref in entries or ref in calcs or ref == "经验"

    def fix(m: re.Match[str]) -> str:
        nonlocal changed
        sentence = m.group(0)
        if not sentence.strip() or "<" in sentence or "⟦" in sentence:
            return sentence
        s2 = re.sub(r"〔(K\d{2,4}|C\d{1,3})〕", lambda r: r.group(0) if known(r.group(1)) else "", sentence)
        if not _sentence_problems(s2, entries=entries, calcs=calcs, own=own):
            changed = changed or s2 != sentence
            return s2
        if EXPERIENCE in s2:  # 经验句里没出处的热量：照旧退回（§256）
            changed = changed or s2 != sentence
            return s2
        bare = ANY_REF.sub("", s2)
        pool = [v for r in KB_REF.findall(s2) if r in entries for v in written_numbers(entries[r].as_evidence())]
        pool += _calc_numbers(s2, calcs) + own
        loose = [v for v in written_numbers(bare) if not traced(v, pool)]
        mark = _source_for(bare, loose, cited=cited, entries=entries, calcs=calcs, support=support) if loose else None
        body = s2.rstrip("\n")
        tail = s2[len(body) :]
        if mark is not None and mark.startswith("<cite>"):
            if ANY_REF.search(s2):  # 已经标了资料或计算、另一半是他的数据：说不准，照旧退回
                changed = changed or s2 != sentence
                return s2
            lead = _LEAD.match(body)
            n = lead.end() if lead else 0  # 列表的"- "、"1. "留在标记外面
            changed = True
            return f"{body[:n]}<statement>{body[n:]}{mark}</statement>{tail}"
        if mark is not None:
            out = body + mark + tail
        elif not _ABOUT_HIM.search(bare) and not _energy(bare):
            out = _label_experience(s2)  # 常规的经验数：直接标经验，不退回（§274）
        else:
            changed = changed or s2 != sentence
            return s2
        if _sentence_problems(out, entries=entries, calcs=calcs, own=own):
            changed = changed or s2 != sentence
            return s2
        changed = True
        return out

    held: list[str] = []

    def hold(m: re.Match[str]) -> str:
        nonlocal changed
        whole = m.group(0)
        seg = next((x for x in parse(whole) if x.statement), None)
        if seg is not None and seg.cites and all(c not in support for c in seg.cites):
            changed = True  # 引了不存在的数据行：拆掉标记，按普通句子补出处（§274）
            return seg.text
        held.append(whole)
        return _tag(len(held) - 1)

    body = _SENTENCE.sub(fix, _STATEMENT.sub(hold, text))  # 带数据编号的句子不动
    if not changed:
        return text
    for i, whole in enumerate(held):
        body = body.replace(_tag(i), whole, 1)
    return body


def _tag(i: int) -> str:
    """占位符里不能有数字（会被当成句子里的数字），用字母编号：⟦Sa⟧、⟦Sb⟧ … ⟦Sba⟧。"""
    letters = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        letters = "abcdefghijklmnopqrstuvwxyz"[r] + letters
    return f"⟦S{letters}⟧"


def cited_ids(text: str) -> list[str]:
    """文字里引用到的全部依据编号（资料 K、计算 C、数据行 r），按出现顺序。"""
    from mirobody.kernel.citations import parse

    ids = [i.strip("〔〕") for i in ANY_REF.findall(text) if i != EXPERIENCE]
    for seg in parse(text):
        ids.extend(seg.cites)
    return list(dict.fromkeys(ids))


_CITE = re.compile(r"<cite>.*?</cite>", re.S)
_STATEMENT_TAG = re.compile(r"</?statement>")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([，。！？；：、）])")
# 核对时删掉一句后，前后的标点会挨在一起（"提醒一句：，先别…""…上限；。"）：留后面那个句末标点
_LOOSE_PUNCT = re.compile(r"[，、；：,]+([。！？；])|([，、；：])[，、；：,]+|([。！？])[。]+")


_ECHO = re.compile(r"([^。；！？\n<>]*?)<statement>(.*?)(<cite>.*?</cite>)</statement>", re.S)


def merge_echoes(text: str) -> str:
    """模型常把同一件事写两遍：先一句普通的话，后面紧跟一个带数据编号的 <statement> 当作"出处"
    （"空腹血糖 5.1，参考范围 3.9–6.1，在范围内<statement>空腹血糖 5.1，参考范围 3.9–6.1<cite>[r3]</cite></statement>"）。
    前面那半句的数字都在紧跟的标记里时，把前面那半句当作带编号的数据句，去掉重复的标记（spec [2026-10-23] §37）。
    标记里只有编号、句子写在外面（"……参考范围 3.9–6.1<statement><cite>[r3]</cite></statement>"）也照此合并。"""

    def fix(m: re.Match[str]) -> str:
        before, inner, cite = m.group(1), m.group(2), m.group(3)
        said = set(written_numbers(before))
        if not said or (inner.strip() and not said <= set(written_numbers(inner))):
            return m.group(0)
        lead = len(before) - len(before.lstrip())
        return f"{before[:lead]}<statement>{before[lead:].rstrip()}{cite}</statement>"

    return _ECHO.sub(fix, text)


# 其他内部编号（记忆条目〔m20〕、数据行〔r3〕等）也不给用户看
_ANY_MARK = re.compile(r"〔[^〔〕\n]{1,12}〕")


def plain(text: str) -> str:
    """给用户看的正文：去掉出处标记、内部编号和数据引用的标签，只留话本身。"""
    out = _STATEMENT_TAG.sub("", _CITE.sub("", _ANY_MARK.sub("", ANY_REF.sub("", text))))
    out = _SPACE_BEFORE_PUNCT.sub(r"\1", out).replace("（）", "").replace("()", "")
    return _LOOSE_PUNCT.sub(lambda m: m.group(1) or m.group(2) or m.group(3), out).strip()


def loose_kinds(
    text: str,
    *,
    entries: Mapping[str, Entry],
    calcs: Mapping[str, str],
    support: Mapping[str, Sequence[float]],
    question: str,
    found: Sequence[str] = (),
) -> dict[str, int]:
    """初稿里对不上的数各属于哪一类（§256，运行记录只记类别和个数，不记原文）：
    given 这一轮系统给过（计算、资料、数据行、工具结果里有这个数）；derived 能由这句里对上的数相加减算出；
    none 哪都没有（多半是凭记忆写的）。"""
    from collections import Counter

    own = written_numbers(question)
    given = [*own, *(v for c in calcs.values() for v in written_numbers(c)),
             *(v for e in entries.values() for v in written_numbers(e.as_evidence())),
             *(v for vals in support.values() for v in vals), *(v for t in found for v in written_numbers(t))]  # fmt: skip
    out: Counter[str] = Counter()
    for sentence in sentences(_STATEMENT.sub("", text)):
        if not _sentence_problems(sentence, entries=entries, calcs=calcs, own=own):
            continue
        refs = [r for r in KB_REF.findall(sentence) if r in entries]
        pool = [v for r in refs for v in written_numbers(entries[r].as_evidence())] + own
        pool += _calc_numbers(sentence, calcs)
        written = _energy(sentence) if EXPERIENCE in sentence else written_numbers(ANY_REF.sub("", sentence))
        loose = [v for v in written if not traced(v, pool)]
        known = [v for v in written if traced(v, given)]
        for v in loose:
            out["given" if traced(v, given) else "derived" if not _derived([v], known) else "none"] += 1
    return dict(out)
