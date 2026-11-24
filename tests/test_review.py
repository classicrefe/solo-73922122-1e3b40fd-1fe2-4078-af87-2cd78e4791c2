"""抄表复核（归零表计校正）的求解器、解析与网页端到端测试。"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="pump-review-test-")
os.environ["PUMP_DB"] = os.path.join(_tmp_dir, "test.db")
os.environ["FLASK_SECRET_KEY"] = "test-secret"

from app import create_app, services  # noqa: E402
from app.db import connect  # noqa: E402
from app.meterreview import parse_review_csv, solve_review, witness  # noqa: E402

LEDGER_CSV = """泵号,入册累计运行小时,出册累计运行小时,结束方式
P001,0,1000,故障
P002,0,1000,预防更换
P003,0,2000,观察结束
P004,500,2000,故障
P005,1000,3000,观察结束
P006,0,3000,预防更换
"""

# M=1000：真实累计 120/340/1080/1150/1260，入册行钉 1080 ⇒ 两端均唯一
UNIQUE_CSV = """表显小时,距上次抄表最多运行小时,标记,核准累计工时
120,,,
340,500,,
80,900,入册,1080
150,400,,
260,200,出册,
"""

# 末行上限放宽到 1200 ⇒ 出册可为 1260 或 2260，不唯一
RANGE_CSV = """表显小时,距上次抄表最多运行小时,标记,核准累计工时
120,,,
340,500,,
80,900,入册,1080
150,400,,
260,1200,出册,
"""

# 核准值钉在出册行，入册行由全部读数反推
EXIT_PIN_CSV = """表显小时,距上次抄表最多运行小时,标记,核准累计工时
120,,,
340,500,,
80,900,入册,
150,400,,
260,200,出册,1260
"""

# 核准值 1085 ÷ 1000 余 85 ≠ 表显 80
CONFLICT_MOD_CSV = UNIQUE_CSV.replace("入册,1080", "入册,1085")

# 表显 340→80 至少需运行 740 小时，上限只填 700
CONFLICT_GAP_CSV = UNIQUE_CSV.replace("80,900,入册", "80,700,入册")

# 入册钉 1080、出册钉 3260：按上限出册只能到 2260
CONFLICT_PINS_CSV = UNIQUE_CSV.replace("260,200,出册,", "260,200,出册,3260")

# 表显 1200 ≥ M=1000，余数不可能等于表显
DISPLAY_GE_M_CSV = UNIQUE_CSV.replace("120,,,", "1200,,,")

# 大数值：M 与工时都取 9 位上限附近
BIG_CSV = """表显小时,距上次抄表最多运行小时,标记,核准累计工时
0,,入册,0
999999998,999999999,出册,
"""

# 增量为 0 的边界：两次抄表之间没有运行
ZERO_INC_CSV = """表显小时,距上次抄表最多运行小时,标记,核准累计工时
10,,入册,10
10,0,出册,
"""

# 跨零边界：增量恰好等于上限
WRAP_EDGE_CSV = """表显小时,距上次抄表最多运行小时,标记,核准累计工时
90,,入册,90
10,20,出册,
"""


def check_assignment(test_case, m, rows, hours):
    """独立校验一组累计工时满足全部约束。"""
    test_case.assertEqual(len(hours), len(rows))
    for i, row in enumerate(rows):
        test_case.assertEqual(hours[i] % m, row.display, f"第 {row.line_no} 行余数")
        test_case.assertGreaterEqual(hours[i], 0)
        if i > 0:
            inc = hours[i] - hours[i - 1]
            test_case.assertGreaterEqual(inc, 0, f"第 {row.line_no} 行增量为负")
            test_case.assertLessEqual(inc, row.max_run, f"第 {row.line_no} 行增量超上限")
        if row.approved is not None:
            test_case.assertEqual(hours[i], row.approved, f"第 {row.line_no} 行核准值")


class SolveTests(unittest.TestCase):
    def test_unique_both_ends(self):
        rows = parse_review_csv(UNIQUE_CSV)
        r = solve_review(1000, rows)
        self.assertTrue(r.feasible)
        self.assertTrue(r.unique)
        self.assertEqual((r.entry_min, r.entry_max), (1080, 1080))
        self.assertEqual((r.exit_min, r.exit_max), (1260, 1260))
        self.assertEqual(
            [(row.min_hours, row.max_hours) for row in r.rows],
            [(120, 120), (340, 340), (1080, 1080), (1150, 1150), (1260, 1260)])

    def test_range_exit_not_unique(self):
        rows = parse_review_csv(RANGE_CSV)
        r = solve_review(1000, rows)
        self.assertTrue(r.feasible)
        self.assertFalse(r.unique)
        self.assertEqual((r.entry_min, r.entry_max), (1080, 1080))
        self.assertEqual((r.exit_min, r.exit_max), (1260, 2260))

    def test_approved_on_exit_only(self):
        rows = parse_review_csv(EXIT_PIN_CSV)
        r = solve_review(1000, rows)
        self.assertTrue(r.unique)
        self.assertEqual((r.entry_min, r.entry_max), (1080, 1080))
        self.assertEqual((r.exit_min, r.exit_max), (1260, 1260))

    def test_approved_mod_mismatch_explained(self):
        r = solve_review(1000, parse_review_csv(CONFLICT_MOD_CSV))
        self.assertFalse(r.feasible)
        self.assertTrue(any("余数" in reason for reason in r.reasons))
        self.assertTrue(any("1085" in reason for reason in r.reasons))

    def test_gap_exceeds_limit_explained(self):
        r = solve_review(1000, parse_review_csv(CONFLICT_GAP_CSV))
        self.assertFalse(r.feasible)
        self.assertTrue(any("740" in reason and "700" in reason
                            for reason in r.reasons))

    def test_conflicting_pins_explained(self):
        r = solve_review(1000, parse_review_csv(CONFLICT_PINS_CSV))
        self.assertFalse(r.feasible)
        self.assertTrue(any("核准" in reason for reason in r.reasons))

    def test_display_not_below_m(self):
        r = solve_review(1000, parse_review_csv(DISPLAY_GE_M_CSV))
        self.assertFalse(r.feasible)
        self.assertTrue(any("余数不可能" in reason for reason in r.reasons))

    def test_big_values_no_enumeration(self):
        rows = parse_review_csv(BIG_CSV)
        r = solve_review(999_999_999, rows)
        self.assertTrue(r.unique)
        self.assertEqual(r.entry_min, 0)
        self.assertEqual(r.exit_min, 999_999_998)

    def test_zero_increment_boundary(self):
        r = solve_review(100, parse_review_csv(ZERO_INC_CSV))
        self.assertTrue(r.unique)
        self.assertEqual((r.entry_min, r.exit_min), (10, 10))

    def test_wrap_increment_at_limit(self):
        r = solve_review(100, parse_review_csv(WRAP_EDGE_CSV))
        self.assertTrue(r.unique)
        self.assertEqual((r.entry_min, r.exit_min), (90, 110))

    def test_invalid_inputs_rejected(self):
        rows = parse_review_csv(UNIQUE_CSV)
        with self.assertRaises(ValueError):
            solve_review(0, rows)
        with self.assertRaises(ValueError):
            solve_review(1_000_000_000, rows)  # M 超过 9 位


class ParseTests(unittest.TestCase):
    def test_missing_column(self):
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv("表显小时,标记\n1,入册\n2,出册\n")
        self.assertIn("缺少必需列", str(ctx.exception))

    def test_first_row_max_run_must_be_empty(self):
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv(UNIQUE_CSV.replace("120,,,", "120,5,,"))
        self.assertIn("首行", str(ctx.exception))

    def test_second_row_max_run_required(self):
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv(UNIQUE_CSV.replace("340,500,,", "340,,,"))
        self.assertIn("第二行起", str(ctx.exception))

    def test_non_integer_and_ten_digits_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv(UNIQUE_CSV.replace("340,500,,", "340,12.5,,"))
        self.assertIn("非负整数", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv(UNIQUE_CSV.replace("340,500,,", "340,1000000000,,"))
        self.assertIn("非负整数", str(ctx.exception))

    def test_approved_only_on_marked_rows(self):
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv(UNIQUE_CSV.replace("340,500,,", "340,500,,500"))
        self.assertIn("只能填在入册/出册行", str(ctx.exception))

    def test_entry_must_precede_exit(self):
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv(UNIQUE_CSV.replace("入册", "出册").replace("出册,\n", "入册,\n"))
        self.assertIn("先入册、后出册", str(ctx.exception))

    def test_exactly_one_entry_and_one_exit(self):
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv(UNIQUE_CSV.replace("260,200,出册,", "260,200,入册,"))
        self.assertIn("出册", str(ctx.exception))

    def test_at_least_one_approved(self):
        with self.assertRaises(ValueError) as ctx:
            parse_review_csv(UNIQUE_CSV.replace("入册,1080", "入册,"))
        self.assertIn("至少一行", str(ctx.exception))

    def test_english_headers_and_marks(self):
        text = ("display,max_run,mark,approved\n"
                "120,,entry,120\n340,500,exit,\n")
        rows = parse_review_csv(text)
        self.assertEqual([r.mark for r in rows], ["entry", "exit"])
        r = solve_review(1000, rows)
        self.assertTrue(r.unique)
        self.assertEqual((r.entry_min, r.exit_min), (120, 340))


class WitnessTests(unittest.TestCase):
    def test_all_bounds_attainable(self):
        rows = parse_review_csv(RANGE_CSV)
        r = solve_review(1000, rows)
        self.assertTrue(r.feasible)
        for line, target in ((r.entry_line, r.entry_min), (r.entry_line, r.entry_max),
                             (r.exit_line, r.exit_min), (r.exit_line, r.exit_max)):
            w = witness(1000, rows, line, target)
            self.assertIsNotNone(w, f"端点 {target} 应可达")
            self.assertEqual(w[line - 2], target)
            check_assignment(self, 1000, rows, w)

    def test_impossible_targets_rejected(self):
        rows = parse_review_csv(RANGE_CSV)
        r = solve_review(1000, rows)
        self.assertIsNone(witness(1000, rows, r.exit_line, 2261))  # 余数不符
        self.assertIsNone(witness(1000, rows, r.exit_line, 3260))  # 超出可行区间
        self.assertIsNone(witness(1000, parse_review_csv(CONFLICT_MOD_CSV),
                                  4, 1085))  # 整体无解


class ReviewWebTests(unittest.TestCase):
    def setUp(self):
        self.web_db = os.path.join(_tmp_dir, f"web-{id(self)}-{os.getpid()}.db")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.web_db + suffix):
                os.remove(self.web_db + suffix)
        self.app = create_app(self.web_db)
        self.client = self.app.test_client()
        self._upload_ledger()

    def _upload_ledger(self):
        return self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(LEDGER_CSV.encode("utf-8")), "pumps.csv")},
            content_type="multipart/form-data")

    def _review(self, pump_id="P004", m="1000", csv_text=UNIQUE_CSV):
        return self.client.post(
            "/pumps/review",
            data={"pump_id": pump_id, "m": m,
                  "review_csv": (io.BytesIO(csv_text.encode("utf-8")), "review.csv")},
            content_type="multipart/form-data")

    def _token(self, pump_id="P004"):
        conn = connect(self.web_db)
        try:
            row = conn.execute(
                "SELECT token FROM review_sessions WHERE pump_id = ?",
                (pump_id,)).fetchone()
            return row["token"] if row else None
        finally:
            conn.close()

    def _confirm(self, token, event_type="preventive"):
        return self.client.post(
            "/pumps/review/confirm",
            data={"token": token, "event_type": event_type},
            follow_redirects=True)

    def _pump(self, pump_id="P004"):
        conn = connect(self.web_db)
        try:
            return services.get_pump(conn, pump_id)
        finally:
            conn.close()

    def test_edit_page_has_review_card(self):
        page = self.client.get("/pumps/P004/edit").get_data(as_text=True)
        self.assertIn("抄表复核", page)
        self.assertIn("归零周期", page)

    def test_review_unique_then_confirm(self):
        resp = self._review()
        self.assertEqual(resp.status_code, 200)
        page = resp.get_data(as_text=True)
        self.assertIn("均唯一", page)
        self.assertIn("确认采用", page)
        token = self._token()
        self.assertIsNotNone(token)

        resp = self._confirm(token, event_type="preventive")
        self.assertIn("已按复核结果更正", resp.get_data(as_text=True))
        row = self._pump()
        self.assertEqual(row["entry_hours"], 1080.0)
        self.assertEqual(row["exit_hours"], 1260.0)
        self.assertEqual(row["event_type"], "preventive")
        # 汇总已同步重算：新时点出现在生命表中
        points = {r["t_hours"] for r in self.client.get("/api/lifetable").get_json()}
        self.assertEqual(points, {0.0, 1000.0, 1080.0, 1260.0, 2000.0, 3000.0})
        # 其他泵未受影响
        self.assertEqual(self._pump("P001")["exit_hours"], 1000.0)
        # 复核会话已消费，不能重复使用
        self.assertIn("须重新复核",
                      self._confirm(token).get_data(as_text=True))

    def test_review_range_cannot_confirm(self):
        resp = self._review(csv_text=RANGE_CSV)
        page = resp.get_data(as_text=True)
        self.assertIn("1260 ~ 2260", page)
        self.assertIn("不能确认", page)
        self.assertNotIn('name="token"', page)
        self.assertIsNone(self._token())

    def test_review_conflict_explains(self):
        resp = self._review(csv_text=CONFLICT_MOD_CSV)
        page = resp.get_data(as_text=True)
        self.assertIn("无解", page)
        self.assertIn("余数", page)
        self.assertIsNone(self._token())

    def test_review_bad_csv_400(self):
        resp = self._review(csv_text="表显小时,标记\n1,入册\n2,出册\n")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("缺少必需列", resp.get_data(as_text=True))
        resp = self._review(m="0")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("须大于零", resp.get_data(as_text=True))

    def test_confirm_invalidated_by_self_correction(self):
        self._review()
        token = self._token()
        # 复核后该泵被普通更正 ⇒ 须重新复核
        self.client.post("/pumps/P004/edit", data={
            "pump_id": "P004", "entry_hours": "500",
            "exit_hours": "2100", "event_type": "failure"})
        page = self._confirm(token).get_data(as_text=True)
        self.assertIn("须重新复核", page)
        row = self._pump()
        self.assertEqual(row["exit_hours"], 2100.0)  # 保留更正后的值，未套用复核值

    def test_confirm_invalidated_by_ledger_replace(self):
        self._review()
        token = self._token()
        self._upload_ledger()  # 整表替换
        page = self._confirm(token).get_data(as_text=True)
        self.assertIn("整表替换", page)
        self.assertEqual(self._pump()["exit_hours"], 2000.0)

    def test_confirm_survives_other_pump_correction(self):
        self._review()
        token = self._token()
        # 单独更正其他泵不影响确认
        self.client.post("/pumps/P002/edit", data={
            "pump_id": "P002", "entry_hours": "0",
            "exit_hours": "1234", "event_type": "failure"})
        page = self._confirm(token).get_data(as_text=True)
        self.assertIn("已按复核结果更正", page)
        self.assertEqual(self._pump()["entry_hours"], 1080.0)
        self.assertEqual(self._pump("P002")["exit_hours"], 1234.0)

    def test_confirm_failure_keeps_original_and_session(self):
        self._review()
        token = self._token()

        def boom(_records):
            raise RuntimeError("simulated rebuild failure")

        original = services.build_life_table
        services.build_life_table = boom
        try:
            page = self._confirm(token).get_data(as_text=True)
        finally:
            services.build_life_table = original
        self.assertIn("确认失败", page)
        row = self._pump()  # 原值保留
        self.assertEqual(row["entry_hours"], 500.0)
        self.assertEqual(row["exit_hours"], 2000.0)
        self.assertEqual(self._token(), token)  # 会话未消费，可重试
        page = self._confirm(token).get_data(as_text=True)
        self.assertIn("已按复核结果更正", page)
        self.assertEqual(self._pump()["exit_hours"], 1260.0)

    def test_confirm_bad_event_type_keeps_original(self):
        self._review()
        token = self._token()
        page = self._confirm(token, event_type="爆炸").get_data(as_text=True)
        self.assertIn("确认失败", page)
        self.assertEqual(self._pump()["exit_hours"], 2000.0)
        self.assertEqual(self._token(), token)

    def test_review_and_confirm_for_new_pump(self):
        resp = self._review(pump_id="P100")
        self.assertEqual(resp.status_code, 200)
        token = self._token("P100")
        self.assertIsNotNone(token)
        page = self._confirm(token, event_type="failure").get_data(as_text=True)
        self.assertIn("新增", page)
        row = self._pump("P100")
        self.assertEqual(row["entry_hours"], 1080.0)
        self.assertEqual(row["exit_hours"], 1260.0)
        self.assertEqual(row["event_type"], "failure")
        meta = services.get_meta(connect(self.web_db))
        self.assertEqual(meta["pump_count"], "7")


if __name__ == "__main__":
    unittest.main(verbosity=2)
