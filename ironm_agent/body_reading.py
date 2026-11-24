"""这个人的身体情况（spec [2026-10-23] §265 需求 5）：化验、体检报告、用药有新的时，在后台读一遍写下来；
档案每轮带一两行结论，问答时不现读、不现查。新的来了整份替换（§113），标明"从化验看出来的"。

只本人可见；和 mirobody 对话内容同一种加密；注销账号时随 ironm_ 表一起删。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from ironm_agent.ddl import run_ddl

_log = logging.getLogger("ironm_agent.body")


# 身体情况（§265 需求 4）：指标和报告对人的影响统一说成这些词，训练、饮食、恢复三份基础规程的参考段
# 「身体情况」里每种都有判断规则；归不进这些的记下来（覆盖记录）
STATES: tuple[str, ...] = (
    "恢复差", "能量不足", "代谢偏慢", "代谢偏快", "血糖调控差", "容易低血糖", "血压高", "心律问题",
    "心率不能用来定强度", "携氧差", "肾负担", "肝负担", "尿酸高", "血脂偏高", "骨骼弱", "出血风险",
    "免疫力低", "电解质紊乱", "不宜憋气发力", "怀孕",
)


@dataclass(frozen=True)
class BodyReading:
    line: str  # 档案每轮带的一两行结论
    text: str = ""  # 完整的身体情况（每项是什么、可能意味着什么、抽血条件、影响练和吃的哪些方面、出处）
    states: tuple[str, ...] = ()  # 归到的身体情况（STATES 里的词）
    codes: tuple[str, ...] = ()  # 由读报告认出、要按危险情况处理的（如 ecg_alert，body_risks.py）
    refs: tuple[str, ...] = ()  # 查到的医学资料出处
    fingerprint: str = ""  # 读的时候化验、报告、用药是什么样（变了才重读）
    made: datetime = field(default_factory=lambda: datetime.now().astimezone())

    def json(self) -> dict[str, Any]:
        return {"line": self.line, "text": self.text, "states": list(self.states), "codes": list(self.codes),
                "refs": list(self.refs), "fingerprint": self.fingerprint, "made": self.made.isoformat()}  # fmt: skip

    @staticmethod
    def of(d: dict[str, Any]) -> BodyReading:
        return BodyReading(str(d.get("line") or ""), str(d.get("text") or ""), tuple(d.get("states") or ()),
                           tuple(d.get("codes") or ()), tuple(d.get("refs") or ()), str(d.get("fingerprint") or ""),
                           datetime.fromisoformat(str(d["made"])) if d.get("made") else datetime.now().astimezone())  # fmt: skip


class BodyStore(Protocol):
    async def get(self, user_id: str) -> BodyReading | None: ...
    async def put(self, user_id: str, reading: BodyReading) -> None: ...


@dataclass
class BodyBook:
    """内存版（测试用）。"""

    rows: dict[str, BodyReading] = field(default_factory=dict)

    async def get(self, user_id: str) -> BodyReading | None:
        return self.rows.get(user_id)

    async def put(self, user_id: str, reading: BodyReading) -> None:
        self.rows[user_id] = reading


DDL = """
CREATE TABLE IF NOT EXISTS ironm_body (
    user_id    text PRIMARY KEY,
    content    text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""


class PgBody:
    """数据库版：一人一行，新的整份替换。"""

    def __init__(self) -> None:
        self._ready = False

    async def _q(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from mirobody.utils.db import execute_query

        if not self._ready:
            await run_ddl(DDL)
            self._ready = True
        rows = await execute_query(sql, params, log_sql=False)
        return rows if isinstance(rows, list) else []

    async def get(self, user_id: str) -> BodyReading | None:
        got = await self._q("SELECT decrypt_content(content) AS content FROM ironm_body WHERE user_id = :uid",
                            {"uid": str(user_id)})  # fmt: skip
        return BodyReading.of(json.loads(got[0]["content"])) if got and got[0].get("content") else None

    async def put(self, user_id: str, reading: BodyReading) -> None:
        await self._q(
            "INSERT INTO ironm_body (user_id, content) VALUES (:uid, encrypt_content(:c))"
            " ON CONFLICT (user_id) DO UPDATE SET content = EXCLUDED.content, updated_at = now() RETURNING 1",
            {"uid": str(user_id), "c": json.dumps(reading.json(), ensure_ascii=False)},
        )


_default: BodyStore | None = None


def default_body() -> BodyStore:
    global _default
    if _default is None:
        _default = PgBody()
    return _default


# ---- 后台读一遍（§265 需求 5）--------------------------------------------------------------------
# 化验、体检报告、用药有新的（指纹变了）才读；问答时不读。分两次调模型：先说要查哪些医学资料（英文标准名），
# 代码去查；再照查到的资料写身体情况。医学说法只来自查到的资料，查不到的写"资料里没查到，建议问医生"。

REPORT_DAYS = 365  # 读一年内的报告
REPORT_MAX = 3  # 最多读几份
REPORT_CHARS = 8000  # 每份的防异常上限；超了按重要性取（attachments.fit_report，§260、§267），不截头
QUERY_MAX = 6  # 最多查几次医学资料
SOURCE_CHARS = 16000  # 查到的资料一共的上限；超了每个查询词平分，不按先后截掉后面的（§267）
LINE_MAX = 160
TEXT_MAX = 6000  # 完整身体情况的防异常上限，超了按条去掉、不截半条（§267）
CODES = frozenset({"ecg_alert"})  # 读报告能认出的、要当危险情况处理的
# 是体检报告或化验单的文件（不是截图、训练视频这类）
REPORT_WORDS = re.compile(r"心电图|超声|彩超|B\s*超|骨密度|DXA|胸片|眼底|总检|体检结论|检查结论|诊断意见|检查所见"
                          r"|参考范围|参考值|检验报告|化验|体检报告|Laboratory Report", re.I)  # fmt: skip


@dataclass(frozen=True)
class Report:
    key: str
    day: date
    text: str
    digest: str = ""


async def recent_reports(user_id: str, today: date) -> list[Report]:
    """他传过的体检报告、化验单（一年内、最近几份，原文前几千字）。"""
    from mirobody.utils.db import execute_query

    rows = await execute_query(
        "SELECT file_key, created_at, content_hash, decrypt_content(original_text) AS text FROM th_files"
        " WHERE user_id = :uid AND is_del = false AND original_text IS NOT NULL AND created_at >= :since"
        " ORDER BY created_at DESC LIMIT 20",
        {"uid": str(user_id), "since": today - timedelta(days=REPORT_DAYS)},
        log_sql=False,
    ) or []
    out: list[Report] = []
    for r in rows:
        text = str(r.get("text") or "")
        if not REPORT_WORDS.search(text):
            continue
        at = r.get("created_at")
        day = at.date() if isinstance(at, datetime) else today
        from ironm_agent.attachments import fit_report

        out.append(Report(str(r.get("file_key") or ""), day, fit_report(text, REPORT_CHARS), str(r.get("content_hash") or "")))
        if len(out) >= REPORT_MAX:
            break
    return out


def fingerprint(labs: Sequence[Any], reports: Sequence[Report], meds: str) -> str:
    """化验、报告、用药现在是什么样；变了才重读。都没有时为空。"""
    lab_rows = sorted((str(x.name), str(x.value), x.day.isoformat() if x.day else "") for x in labs)
    rep_rows = sorted((x.key, x.digest or hashlib.sha1(x.text.encode()).hexdigest()) for x in reports)
    if not lab_rows and not rep_rows and not meds.strip():
        return ""
    raw = json.dumps([lab_rows, rep_rows, meds.strip()], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()


def _labs_text(labs: Sequence[Any]) -> str:
    out = []
    for x in labs:
        ref = f"参考 {x.ref}" if getattr(x, "ref", "") else "单子上没印参考范围"
        verdict = f"，单子标的或按范围比是{x.verdict}" if getattr(x, "verdict", "") else ""
        std = f"（{x.std}）" if getattr(x, "std", "") and x.std != x.name else ""
        when = x.day.isoformat() if x.day else "日期不明"
        out.append(f"- {x.name}{std}：{x.value}{(' ' + x.unit) if x.unit else ''}，{ref}{verdict}，{when}")
    return "\n".join(out)


_RULES = """规矩（照 mirobody）：不诊断、不编数字，可能性不说成确定（"可能和……有关"）；只按化验单印的参考范围说偏高偏低，没印就只说变化；
医学上的说法只来自下面"查到的资料"，没查到就写"资料里没查到，建议问医生"，不凭印象补；说明书上的剂量不是给他本人的建议。
IronM 补的：抽血条件和方法变更、用药、病程一起看，有证据才归到抽血条件（抽血前几天练过大重量会让转氨酶、肌酸激酶、肌酐偏高；吃肌酸让肌酐偏高；没空腹让甘油三酯、血糖偏高；
喝酒、快速掉体重让尿酸偏高；吃生物素让甲功失真）；指标互相参照（贫血时糖化不可信、肌肉多时肌酐偏高看 eGFR）；危险的一定标出来。"""

TERMS_PROMPT = f"""你在后台替健身教练 IronM 读一个人的化验、体检报告和用药，先决定要查哪些医学资料。
只列偏高、偏低、接近边界的指标，报告里写了异常的结论（心电图、B 超、骨密度等），和在用的药；用英文标准名或英文医学词，
不写名字、数值、日期。最多 {QUERY_MAX} 个。只输出 JSON：{{"queries": ["uric acid", "ALT", "ST segment depression", "levothyroxine"]}}；没有要查的给空数组。"""

READ_PROMPT = f"""你在后台替健身教练 IronM 读一个人的化验、体检报告和用药，写他的"身体情况"，给以后每轮作答的模型拿主意用（不直接给他看）。
{_RULES}
身体情况只能从这些词里选：{"、".join(STATES)}。states 只写会持续一段时间的（结果确定、在治疗、长期用药、多次化验都这样）——
排计划、出饮食方案会按它调；只偏一点又没别的印证、抽血条件可疑、等复查的，不写进 states，写在 caution 里（§269）。
只写偏高、偏低、接近边界的项和报告里写了的异常、医生建议；都正常就说一句都在范围内。
只输出 JSON：
{{"items": [{{"what": "尿酸 520 μmol/L（参考 208–428，11-03）", "meaning": "照查到的资料说对人的影响", "caution": "抽血条件要不要打折看；没有就空",
  "states": ["尿酸高"], "aspects": "影响练和吃的哪些方面（一句）", "refs": ["资料编号"], "doctor": "要不要看医生、哪个科；不用就空"}}],
 "line": "一两行结论，给每轮带：最要紧的几项、对应的身体情况和练吃上要注意的，不超过 {LINE_MAX} 字",
 "codes": ["心电图有需要先看医生再加大强度的改变时写 ecg_alert，否则空"],
 "unmapped": ["异常但归不进上面那些身体情况的项"], "unknown": ["报告里认不出意思的结论原话"]}}"""


def _sources_text(found: Sequence[tuple[str, str]]) -> str:
    """每个查询词查到的资料都带上；放不下时每个查询词平分篇幅，只去掉各自靠后的段落并注明，
    免得靠后的指标被当成"没查到"（§267）。"""
    if not found:
        return ""
    share = SOURCE_CHARS // len(found)
    out = []
    for q, text in found:
        body = text.strip()
        if len(body) > share:
            parts, kept = body.split("\n\n"), []
            for p in parts:  # 按段落（每段一条资料）取，不截半段
                if sum(len(x) + 2 for x in kept) + len(p) > share and kept:
                    break
                kept.append(p[:share])
            body = "\n\n".join(kept) + "\n（这个词还查到别的资料，放不下略去；不算没查到）"
        out.append(f"【{q}】\n{body}")
    return "\n\n".join(out)


_REF = re.compile(r"\[?ref:([a-z_]+:[A-Za-z0-9#_.-]+)\]?|\b((?:medlineplus(?:_test)?|openfda|pmid|nct):[A-Za-z0-9#_.-]+)")


@dataclass(frozen=True)
class Gaps:
    unmapped: tuple[str, ...] = ()  # 异常但归不进身体情况的
    unknown: tuple[str, ...] = ()  # 报告里认不出的结论
    not_found: tuple[str, ...] = ()  # 查不到资料的
    states: tuple[str, ...] = ()  # 模型写了、但不在身体情况里的词


def _strs(x: Any, limit: int = 12) -> tuple[str, ...]:
    return tuple(str(v).strip()[:80] for v in (x if isinstance(x, list) else []) if str(v).strip())[:limit]


def render(raw: Mapping[str, Any], fp: str, known_refs: set[str]) -> tuple[BodyReading, Gaps]:
    """模型写的 → 存下的身体情况；身体情况只认 STATES 里的词，代码只认 CODES 里的，出处只认查到过的。"""
    lines, states, refs, bad_states = [], [], [], []
    for x in raw.get("items") or []:
        if not isinstance(x, Mapping) or not str(x.get("what") or "").strip():
            continue
        got = [s for s in _strs(x.get("states")) if s in STATES]
        bad_states += [s for s in _strs(x.get("states")) if s not in STATES]
        states += got
        rs = [r for r in (re.sub(r"^\[?(?:ref:)?|\]$", "", v) for v in _strs(x.get("refs"))) if r in known_refs]
        refs += rs
        parts = [str(x.get("what")).strip()]
        for key, head in (("meaning", ""), ("caution", "抽血条件："), ("aspects", "练和吃："), ("doctor", "看医生：")):
            v = str(x.get(key) or "").strip()
            if v:
                parts.append(head + v)
        tag = f"（身体情况：{'、'.join(got)}）" if got else ""
        src = f"〔资料 {'、'.join(rs)}〕" if rs else ""
        lines.append("；".join(parts) + tag + src)
    line = " ".join(str(raw.get("line") or "").split())[:LINE_MAX]
    codes = tuple(c for c in _strs(raw.get("codes")) if c in CODES)
    kept: list[str] = []
    for x in lines:  # 按条放，放不下的整条去掉（§267），不截半条
        if sum(len(y) + 3 for y in kept) + len(x) + 3 > TEXT_MAX:
            break
        kept.append(x)
    text = "\n".join(f"- {x}" for x in kept)
    reading = BodyReading(line or ("化验和报告都在范围内" if not lines else lines[0][:LINE_MAX]), text,
                          tuple(dict.fromkeys(states)), codes, tuple(dict.fromkeys(refs)), fp)  # fmt: skip
    return reading, Gaps(_strs(raw.get("unmapped")), _strs(raw.get("unknown")), (), tuple(dict.fromkeys(bad_states)))


async def read(
    llm: Any,
    *,
    labs: Sequence[Any],
    reports: Sequence[Report],
    meds: str,
    about: str,
    context: str,
    search: Callable[[str, str], Awaitable[str]] | None,
    today: date,
) -> tuple[BodyReading, Gaps] | None:
    """读一遍：先列要查的资料、代码去查，再照资料写身体情况。模型不可用或输出不对时返回 None。"""
    from ironm_agent.llm import ModelUnavailable, parse_json

    fp = fingerprint(labs, reports, meds)
    data = "\n\n".join(x for x in (
        f"今天是 {today.isoformat()}。{about}",
        "化验（每项最近一次）：\n" + _labs_text(labs) if labs else "",
        "\n\n".join(f"报告（{r.day.isoformat()} 传的）：\n{r.text}" for r in reports),
        f"在用的药（mirobody 用药记录）：\n{meds.strip()}" if meds.strip() else "",
        f"抽血前后练了什么、吃不吃补剂（判断抽血条件用）：\n{context}" if context else "",
    ) if x)  # fmt: skip
    found: list[tuple[str, str]] = []
    not_found: list[str] = []
    try:
        if search is not None:
            reply = await llm.chat("memory", [{"role": "system", "content": TERMS_PROMPT},
                                              {"role": "user", "content": data}], json_mode=True)  # fmt: skip
            from ironm_agent.medical_terms import english

            queries = [english(q) for q in _strs((parse_json(reply.content) or {}).get("queries"), QUERY_MAX)
                       if not re.search(r"\d{2,}", q)]  # 不带数值；中文换成英文医学词，换不了的词不发出去
            queries = [q for q in dict.fromkeys(queries) if q]
            for q in queries:
                try:
                    got = await search(q, "")
                except Exception as e:
                    _log.warning("body search failed: error_type=%s", type(e).__name__)
                    got = ""
                if re.match(r"\d+ passages", got or ""):  # 查到了（没装、没查到、连不上都是别的说法）
                    found.append((q, got))
                else:
                    not_found.append(q)
        sources = _sources_text(found)
        messages = [{"role": "system", "content": READ_PROMPT},
                    {"role": "user", "content": data + "\n\n查到的资料（引用时写资料编号，如 medlineplus_test:uric-acid-test）：\n"
                     + (sources or "（没有查到资料，医学说法一律写资料里没查到、建议问医生）")}]  # fmt: skip
        reply = await llm.chat("memory", messages, json_mode=True)
    except ModelUnavailable:
        return None
    raw = parse_json(reply.content)
    if not isinstance(raw, Mapping):
        return None
    known = {m.group(1) or m.group(2) for m in _REF.finditer(sources)}
    reading, gaps = render(raw, fp, known)
    return reading, Gaps(gaps.unmapped, gaps.unknown, tuple(not_found), gaps.states)
