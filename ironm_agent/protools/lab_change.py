"""两次化验的变化和正常波动比（spec [2026-10-23] §271 需求 2）。

变化多少、两次测量的参考变化量（RCV）由代码算，是不是真变了、为什么变由作答的模型判断。
RCV 按小变异的对数近似：每次的总波动 CV = √(CVI² + CVA²)，双侧 95% RCV = 1.96 × √(CV前² + CV后²)
（两次同一方法时等于 √2 × 1.96 × √(CVI² + CVA²)，Fraser 的写法）；观测变化用 ln(后/前)。
不内置各项指标的波动参数：用他给的规程、报告里写的。
"""

from __future__ import annotations

import math

from ironm_agent.protools.result import Finding, Result

Z95 = 1.96
SOURCE = "参考变化量 RCV = 1.96 × √(CV前² + CV后²)，CV = √(个体内波动² + 检测误差²)（Fraser，Biological Variation 的 RCV）"
NO_SOURCE_ROLE = "参考变化量只说变化幅度，不单独决定来源：采样条件、检测方法、用药、病程有证据的照证据判。"


def _pct(x: float) -> str:
    return f"{x:+.1f}%"


def lab_change(
    name: str,
    before: float | None,
    after: float | None,
    *,
    cv_i: float | None = None,
    cv_a: float | None = None,
    cv_a_before: float | None = None,
    cv_a_after: float | None = None,
    rcv: float | None = None,
) -> Result:
    if before is None or after is None or before <= 0 or after <= 0:
        return Result.note("要两次读数（都大于 0）才能算变化：before 写上次，after 写这次。")
    what = name.strip() or "这项"
    change = (after - before) / before * 100
    log = math.log(after / before) * 100
    data = f"{what} 上次 {before:g}，这次 {after:g}"
    moved = f"{what}从 {before:g} 到 {after:g}，变了 {_pct(change)}（按对数算 {_pct(log)}）"
    a0 = cv_a_before if cv_a_before is not None else cv_a
    a1 = cv_a_after if cv_a_after is not None else cv_a
    how = "变化 = (这次 − 上次) ÷ 上次；对数变化 = ln(这次 ÷ 上次)"
    if rcv is None and cv_i is not None and a0 is not None and a1 is not None:
        if min(cv_i, a0, a1) < 0:
            return Result.note("波动参数是百分数，不能是负的。")
        rcv = Z95 * math.sqrt(cv_i**2 + a0**2 + cv_i**2 + a1**2)
        same = "两次同一方法" if a0 == a1 else f"两次方法不同，检测误差各 {a0:g}% 和 {a1:g}%"
        how += f"；参考变化量按个体内波动 {cv_i:g}%、检测误差 {a0:g}%/{a1:g}% 算（{same}）"
    elif rcv is not None:
        how += "；参考变化量用他给的规程或报告里写好的数"
    if rcv is None or rcv <= 0:
        return Result(
            findings=(Finding("化验变化", moved, how, data),),
            notes=("没有这项的波动参数（个体内波动、检测误差或参考变化量），不能判断是不是正常波动：看他给的规程、"
                   "报告里有没有；没有就只说变了多少。",),
        )  # fmt: skip
    inside = abs(log) <= rcv
    edge = abs(abs(log) - rcv) < 1.0
    verdict = ("没超过，在测量误差和个体正常波动以内" if inside else "超过了，不只是测量误差和个体波动") + (
        "（离界限不到 1 个百分点，说不准）" if edge else ""
    )
    text = f"{moved}；两次测量的参考变化量 ±{rcv:.1f}%，{verdict}"
    return Result(findings=(Finding("化验变化和正常波动比", text, how, data, SOURCE),), notes=(NO_SOURCE_ROLE,))
