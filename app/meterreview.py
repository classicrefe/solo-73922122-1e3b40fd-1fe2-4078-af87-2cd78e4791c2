"""抄表复核：归零表计的累计工时恢复（区间传播，不按圈数逐一试算）。

工时表每满 M 小时归零，抄表间隔里可能漏过整圈。按抄表顺序给定表显读数
d_1..d_n，以及第 i 次抄表距上次抄表最多运行小时 u_i（i ≥ 2）。真实累计
工时 C_i 满足：

    C_i ≡ d_i (mod M)         累计工时除以 M 的余数等于表显
    0 ≤ C_i − C_{i−1} ≤ u_i   相邻增量在 0 到所填上限之间

入册行、出册行至少一行附核准累计工时 A（钉值 C = A）。

求解不枚举圈数：令 C_i = d_i + M·k_i（k_i ≥ 0 为整圈数），约束化为链式
差分约束

    k_i − k_{i−1} ∈ [lo_i, hi_i]
    lo_i = 0（d_i ≥ d_{i−1}，未跨零）或 1（d_i < d_{i−1}，至少绕满一圈）
    hi_i = floor((u_i + d_{i−1} − d_i) / M)

链式差分约束中每个变量的最紧界就是沿链的最远传播：一次前向、一次后向
区间传播（钉值就地相交）即得每个 k_i 的精确可行区间，入册/出册各自的
最小、最大工时直接读出。复杂度 O(n)。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

MAX_HOURS = 999_999_999  # 工时数据：至多 9 位的非负整数

_UINT_RE = re.compile(r"[0-9]{1,9}")

MARK_LABELS = {"entry": "入册", "exit": "出册"}

_MARK_ALIASES = {
    "入册": "entry", "entry": "entry",
    "出册": "exit", "exit": "exit",
}

_HEADER_ALIASES = {
    "display": {"表显小时", "表显", "表显读数", "表显值", "display", "reading", "meter"},
    "max_run": {
        "距上次抄表最多运行小时", "距上次最多运行小时", "最多运行小时",
        "运行上限", "max_run", "maxrun", "maxhours",
    },
    "mark": {"标记", "出入册", "角色", "mark", "role"},
    "approved": {"核准累计工时", "核准工时", "核准累计小时", "approved", "approvedhours"},
}

REQUIRED_COLUMNS = ("display", "max_run", "mark", "approved")


@dataclass(frozen=True)
class ReviewRow:
    line_no: int          # CSV 行号（表头为第 1 行）
    display: int          # 表显小时 d
    max_run: int | None   # 距上次抄表最多运行小时（首行为 None）
    mark: str | None      # 'entry' / 'exit' / None
    approved: int | None  # 核准累计工时（仅入册/出册行可填）


@dataclass
class RowRange:
    line_no: int
    display: int
    max_run: int | None
    mark: str | None
    approved: int | None
    min_hours: int        # 该行可达的最小累计工时
    max_hours: int        # 该行可达的最大累计工时


@dataclass
class ReviewResult:
    m: int
    feasible: bool
    reasons: list[str]        # 无解时的说明
    rows: list[RowRange]      # 有解时逐行可行区间
    entry_line: int | None
    exit_line: int | None
    entry_min: int | None
    entry_max: int | None
    exit_min: int | None
    exit_max: int | None
    unique: bool              # 入册与出册均唯一（此时才可确认）


# --------------------------------------------------------------------- CSV --

def _normalize(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace(" ", "").replace("_", "")


def _map_headers(fieldnames: list[str]) -> dict[str, int]:
    normalized = {_normalize(n): i for i, n in enumerate(fieldnames)}
    aliases = {c: {_normalize(a) for a in aa} for c, aa in _HEADER_ALIASES.items()}
    mapping: dict[str, int] = {}
    missing = []
    for col in REQUIRED_COLUMNS:
        for alias in aliases[col]:
            if alias in normalized:
                mapping[col] = normalized[alias]
                break
        else:
            missing.append(
                f"缺少必需列 {col}（可用表头：{'、'.join(sorted(_HEADER_ALIASES[col]))}）"
            )
    if missing:
        raise ValueError("抄表 CSV 解析失败：\n" + "\n".join(missing))
    return mapping


def _uint(text: str, what: str, line_no: int, errors: list[str]) -> int | None:
    if not _UINT_RE.fullmatch(text):
        errors.append(f"第 {line_no} 行: {what}须为至多 9 位的非负整数，收到: {text!r}")
        return None
    return int(text)


def parse_review_csv(text: str) -> list[ReviewRow]:
    """解析抄表 CSV（按抄表顺序，每行一次抄表）。

    四列：表显小时 / 距上次抄表最多运行小时 / 标记 / 核准累计工时。
    所有问题一次性收集后抛出 ValueError。
    """
    reader = csv.reader(io.StringIO(text))
    raw_rows = [r for r in reader if any(c.strip() for c in r)]
    if not raw_rows:
        raise ValueError("抄表 CSV 文件为空")
    header, data = raw_rows[0], raw_rows[1:]
    mapping = _map_headers(header)
    if len(data) < 2:
        raise ValueError("抄表 CSV 解析失败：\n抄表记录至少需要两行（先入册、后出册各一行）")

    errors: list[str] = []
    rows: list[ReviewRow] = []
    for idx, raw in enumerate(data):
        line_no = idx + 2

        def cell(col: str) -> str:
            i = mapping[col]
            return raw[i].strip() if i < len(raw) else ""

        display = _uint(cell("display"), "表显小时", line_no, errors)

        max_run_raw = cell("max_run")
        max_run = None
        if idx == 0:
            if max_run_raw:
                errors.append(f"第 {line_no} 行: 首行没有上次抄表，距上次最多运行小时须留空")
        elif not max_run_raw:
            errors.append(f"第 {line_no} 行: 从第二行起须填距上次抄表最多运行小时")
        else:
            max_run = _uint(max_run_raw, "距上次抄表最多运行小时", line_no, errors)

        mark_raw = _normalize(cell("mark"))
        mark = None
        if mark_raw:
            mark = _MARK_ALIASES.get(mark_raw)
            if mark is None:
                errors.append(f"第 {line_no} 行: 标记只能填 入册 或 出册，收到: {cell('mark')!r}")

        approved_raw = cell("approved")
        approved = None
        if approved_raw:
            approved = _uint(approved_raw, "核准累计工时", line_no, errors)
            if mark is None and approved is not None:
                errors.append(f"第 {line_no} 行: 核准累计工时只能填在入册/出册行")

        rows.append(ReviewRow(line_no, display if display is not None else 0,
                              max_run, mark, approved))

    if errors:
        raise ValueError("抄表 CSV 解析失败：\n" + "\n".join(errors))

    entries = [i for i, r in enumerate(rows) if r.mark == "entry"]
    exits = [i for i, r in enumerate(rows) if r.mark == "exit"]
    structural = []
    if len(entries) != 1:
        structural.append(f"须且只须标出一行「入册」，实际标了 {len(entries)} 行")
    if len(exits) != 1:
        structural.append(f"须且只须标出一行「出册」，实际标了 {len(exits)} 行")
    if len(entries) == 1 and len(exits) == 1:
        if entries[0] >= exits[0]:
            structural.append("入册行必须先于出册行（按抄表顺序先入册、后出册）")
        elif rows[entries[0]].approved is None and rows[exits[0]].approved is None:
            structural.append("入册/出册两行至少一行须附核准累计工时")
    if structural:
        raise ValueError("抄表 CSV 解析失败：\n" + "\n".join(structural))
    return rows


# ------------------------------------------------------------------ solver --

class _Prop:
    """前向–后向传播后的精确可行区间（k 空间）与边约束。"""

    def __init__(self, lo, hi, edge_lo, edge_hi):
        self.lo = lo            # lo[i]: 第 i 行 k 的可行下界
        self.hi = hi            # hi[i]: 第 i 行 k 的可行上界（有限整数）
        self.edge_lo = edge_lo  # edge_lo[i]: k_i − k_{i−1} 下界
        self.edge_hi = edge_hi  # edge_hi[i]: k_i − k_{i−1} 上界


def _analyze(m: int, rows: list[ReviewRow]) -> tuple[list[str], _Prop | None]:
    """返回 (无解原因列表, 传播结果)。原因非空即无解。"""
    n = len(rows)
    reasons: list[str] = []

    # 表显必须能作为余数；核准值钉死 k
    pins: dict[int, int] = {}
    for i, r in enumerate(rows):
        if r.display >= m:
            reasons.append(
                f"第 {r.line_no} 行: 表显 {r.display} 不小于归零周期 {m}，"
                f"累计工时除以 {m} 的余数不可能等于它")
        if r.approved is not None:
            if r.approved % m != r.display:
                reasons.append(
                    f"第 {r.line_no} 行: 核准累计工时 {r.approved} 除以 {m} 的余数为 "
                    f"{r.approved % m}，与表显 {r.display} 矛盾")
            else:
                pins[i] = r.approved // m
    if reasons:
        return reasons, None

    # 相邻行的差分约束（k 空间）
    edge_lo = [0] * n
    edge_hi = [0] * n
    for i in range(1, n):
        d0, d1 = rows[i - 1].display, rows[i].display
        u = rows[i].max_run
        delta = d1 - d0
        lo = 0 if delta >= 0 else 1
        hi = (u - delta) // m
        edge_lo[i], edge_hi[i] = lo, hi
        if hi < lo:
            reasons.append(
                f"第 {rows[i].line_no} 行: 表显由 {d0} 变为 {d1} 至少需运行 "
                f"{delta % m} 小时，超过所填上限 {u}")
    if reasons:
        return reasons, None

    def hours(i: int, k: int) -> int:
        return rows[i].display + m * k

    # 前向：只由第 1..i 行约束推进（无上界用 None 表示）
    fk_lo = [0] * n
    fk_hi: list[int | None] = [None] * n
    for i in range(n):
        if i > 0:
            fk_lo[i] = max(fk_lo[i - 1] + edge_lo[i], 0)
            fk_hi[i] = None if fk_hi[i - 1] is None else fk_hi[i - 1] + edge_hi[i]
        if i in pins:
            p = pins[i]
            if p < fk_lo[i]:
                reasons.append(
                    f"第 {rows[i].line_no} 行: 核准累计工时 {rows[i].approved} 偏小，"
                    f"由前序读数与上限推进，该行累计工时至少为 {hours(i, fk_lo[i])}")
                return reasons, None
            if fk_hi[i] is not None and p > fk_hi[i]:
                reasons.append(
                    f"第 {rows[i].line_no} 行: 核准累计工时 {rows[i].approved} 偏大，"
                    f"由前序读数与上限推进，该行累计工时至多为 {hours(i, fk_hi[i])}")
                return reasons, None
            fk_lo[i] = fk_hi[i] = p
        if fk_hi[i] is not None and fk_lo[i] > fk_hi[i]:
            reasons.append(
                f"第 {rows[i].line_no} 行: 由前序读数与上限推进，已无可行累计工时")
            return reasons, None

    # 后向：只由第 i..n 行约束回推
    bk_lo = [0] * n
    bk_hi: list[int | None] = [None] * n
    for i in range(n - 1, -1, -1):
        if i < n - 1:
            bk_lo[i] = max(bk_lo[i + 1] - edge_hi[i + 1], 0)
            bk_hi[i] = None if bk_hi[i + 1] is None else bk_hi[i + 1] - edge_lo[i + 1]
        if i in pins:
            p = pins[i]
            if p < bk_lo[i]:
                reasons.append(
                    f"第 {rows[i].line_no} 行: 核准累计工时 {rows[i].approved} 偏小，"
                    f"由后序读数回推，该行累计工时至少为 {hours(i, bk_lo[i])}")
                return reasons, None
            if bk_hi[i] is not None and p > bk_hi[i]:
                reasons.append(
                    f"第 {rows[i].line_no} 行: 核准累计工时 {rows[i].approved} 偏大，"
                    f"由后序读数回推，该行累计工时至多为 {hours(i, bk_hi[i])}")
                return reasons, None
            bk_lo[i] = bk_hi[i] = p
        if bk_hi[i] is not None and bk_lo[i] > bk_hi[i]:
            reasons.append(
                f"第 {rows[i].line_no} 行: 由后序读数回推，已无可行累计工时"
                f"（累计工时为至多 9 位非负整数，不能为负）")
            return reasons, None

    fin_lo = [max(fk_lo[i], bk_lo[i]) for i in range(n)]
    fin_hi = []
    for i in range(n):
        bounds = [b for b in (fk_hi[i], bk_hi[i]) if b is not None]
        if not bounds:  # 至少一个钉值 ⇒ 全链有界；此处仅为防御
            reasons.append("内部错误: 可行区间无界")
            return reasons, None
        fin_hi.append(min(bounds))
    return [], _Prop(fin_lo, fin_hi, edge_lo, edge_hi)


def solve_review(m: int, rows: list[ReviewRow]) -> ReviewResult:
    """综合全部读数与核准值，求入册/出册各自可达的最小、最大工时。"""
    if m <= 0:
        raise ValueError("归零周期 M 须大于零")
    if m > MAX_HOURS:
        raise ValueError(f"归零周期 M 不能超过 {MAX_HOURS}（至多 9 位）")
    if len(rows) < 2:
        raise ValueError("抄表记录至少需要两行")
    for r in rows:
        if not (0 <= r.display <= MAX_HOURS):
            raise ValueError(f"第 {r.line_no} 行表显超出范围")
        if r.max_run is not None and not (0 <= r.max_run <= MAX_HOURS):
            raise ValueError(f"第 {r.line_no} 行运行上限超出范围")
        if r.approved is not None and not (0 <= r.approved <= MAX_HOURS):
            raise ValueError(f"第 {r.line_no} 行核准累计工时超出范围")
    entries = [i for i, r in enumerate(rows) if r.mark == "entry"]
    exits = [i for i, r in enumerate(rows) if r.mark == "exit"]
    if len(entries) != 1 or len(exits) != 1:
        raise ValueError("须且只须标出先入册、后出册两行")
    e, x = entries[0], exits[0]
    if e >= x:
        raise ValueError("入册行必须先于出册行")
    if rows[e].approved is None and rows[x].approved is None:
        raise ValueError("入册/出册两行至少一行须附核准累计工时")

    reasons, prop = _analyze(m, rows)
    if reasons:
        return ReviewResult(m, False, reasons, [], rows[e].line_no, rows[x].line_no,
                            None, None, None, None, False)

    ranges = [
        RowRange(r.line_no, r.display, r.max_run, r.mark, r.approved,
                 r.display + m * prop.lo[i], r.display + m * prop.hi[i])
        for i, r in enumerate(rows)
    ]
    entry_min = rows[e].display + m * prop.lo[e]
    entry_max = rows[e].display + m * prop.hi[e]
    exit_min = rows[x].display + m * prop.lo[x]
    exit_max = rows[x].display + m * prop.hi[x]
    unique = entry_min == entry_max and exit_min == exit_max
    return ReviewResult(m, True, [], ranges, rows[e].line_no, rows[x].line_no,
                        entry_min, entry_max, exit_min, exit_max, unique)


def witness(m: int, rows: list[ReviewRow], line_no: int,
            hours: int) -> list[int] | None:
    """构造一组满足全部约束、且第 line_no 行累计工时等于 hours 的可行解。

    用于校验区间端点确实可达；无法满足时返回 None。
    """
    reasons, prop = _analyze(m, rows)
    if reasons:
        return None
    idx = {r.line_no: i for i, r in enumerate(rows)}.get(line_no)
    if idx is None:
        return None
    d = rows[idx].display
    if hours < d or (hours - d) % m != 0:
        return None
    k = (hours - d) // m
    if not (prop.lo[idx] <= k <= prop.hi[idx]):
        return None
    n = len(rows)
    ks = [0] * n
    ks[idx] = k
    for i in range(idx - 1, -1, -1):
        cand = min(prop.hi[i], ks[i + 1] - prop.edge_lo[i + 1])
        if cand < max(prop.lo[i], ks[i + 1] - prop.edge_hi[i + 1]):
            return None
        ks[i] = cand
    for i in range(idx + 1, n):
        cand = max(prop.lo[i], ks[i - 1] + prop.edge_lo[i])
        if cand > min(prop.hi[i], ks[i - 1] + prop.edge_hi[i]):
            return None
        ks[i] = cand
    return [rows[i].display + m * ks[i] for i in range(n)]
