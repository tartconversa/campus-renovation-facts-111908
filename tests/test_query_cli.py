"""钻取查询与 CLI 冒烟测试。"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

import _path  # noqa: F401  隔离模式路径引导
from factwarehouse.release import build_release
from factwarehouse.query import (
    overview, drill_value, drill_measure, drill_payment, space_report,
    item_report, acceptance_report,
)
from tests.factories import (
    build_source_dir, bill, measure, link, variation, payment,
)

ROOT = Path(__file__).resolve().parents[1]


class QueryDrilldown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        src = Path(cls.tmp.name) / "src"
        cls.state = Path(cls.tmp.name) / "state"
        build_source_dir(
            src,
            [
                bill("K1V1", "L1", 1, "整栋", "", "", 1000000, "2026-10-01"),
                bill("KA", "L1", 2, "通道", "m2", "1000", 480000, "2026-10-15"),
                bill("KB", "L2", 2, "活动", "m2", "2000", 520000, "2026-10-15"),
            ],
            [
                measure("M1", 1, "KA", "S1", "4", "2026-10-16"),
                measure("M2", 1, "KB", "S2", "3", "2026-10-17"),
            ],
            [
                link("A1", "AC1", "M1", "2"),
                link("A2", "AC1", "M2", "3"),
                link("A3", "AC2", "M1", "2"),
                link("A4", "AC3", "M1", "2", supersedes="A3"),
            ],
            [variation("V1", ["K1V1"], ["KA", "KB"], "2026-10-15", False)],
            [
                payment("P1", 30000, ["AC1", "AC2"], "2026-10-30"),
                payment("P2", -5000, ["AC1"], "2026-11-02", reverses="P1"),
            ],
        )
        cls.release_dir, cls.manifest = build_release(src, cls.state, "RQ")
        cls.db = cls.release_dir / "warehouse.sqlite"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_value_drill_chain_to_item_and_price(self):
        rows = drill_value(self.db, acceptance_id="AC1")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(row["price_timeline"])
            self.assertTrue(row["lineage"])  # 拆项血缘可追
            self.assertEqual(row["link_status"], "accepted")

    def test_measure_drill_shows_superseded_link(self):
        payload = drill_measure(self.db, "M1")
        statuses = {c["link_id"]: c["is_active"] for c in payload["coverage"]}
        self.assertEqual(statuses["A3"], 0)
        self.assertEqual(statuses["A4"], 1)
        self.assertEqual(payload["status"]["undisputed_quantity"], "4")

    def test_payment_drill_explains_every_fen(self):
        p1 = drill_payment(self.db, "P1")
        # 守恒恒等式：三类依据分量 + 未解释 = 付款额（本夹具含一条失效依据 A3）
        self.assertEqual(
            p1["recognized_basis_fen"] + p1["inactive_basis_fen"] + p1["unexplained_fen"],
            p1["amount_fen"],
        )
        self.assertEqual(p1["reversed_by"][0]["payment_id"], "P2")
        p2 = drill_payment(self.db, "P2")
        self.assertEqual(p2["reverses_target"]["payment_id"], "P1")
        self.assertGreaterEqual(p2["unexplained_fen"], 0)

    def test_space_report_excludes_lump_sum(self):
        report = space_report(self.db)
        self.assertTrue(report["excluded_non_decomposable"])
        self.assertNotIn("K1V1", [s["item_key"] for s in report["spaces"]])

    def test_item_report_keeps_contract_and_earned_apart(self):
        report = item_report(self.db)
        ka = next(i for i in report["items"] if i["item_key"] == "KA")
        self.assertEqual(ka["contract_total_fen"], 480000)
        self.assertEqual(ka["recognized_value_fen"], 4000)  # 4 × 1000
        self.assertTrue(report["lineage"])

    def test_acceptance_report_many_to_many(self):
        rows = acceptance_report(self.db)
        ac1 = [r for r in rows if r["acceptance_id"] == "AC1"]
        self.assertEqual(len(ac1), 2)  # 一张验收单覆盖两张计量

    def test_overview_boundaries_separated(self):
        result = overview(self.db)
        self.assertIn("recognized", result["value_fen"])
        self.assertEqual(sum(p["amount_fen"] for p in result["cash"]), 25000)
        self.assertEqual(result["lump_non_decomposable_fen"], 1000000)


class CliSmoke(unittest.TestCase):
    def test_cli_build_verify_and_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            proc = subprocess.run(
                [sys.executable, "-I", str(ROOT / "fw.py"), "build",
                 "--root", str(ROOT), "--state", str(state), "--release", "RC"],
                capture_output=True, text=True, check=True,
            )
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["manifest"]["conservation_all_passed"])
            proc2 = subprocess.run(
                [sys.executable, "-I", str(ROOT / "fw.py"), "verify",
                 "--state", str(state), "--release", "RC"],
                capture_output=True, text=True, check=True,
            )
            self.assertTrue(json.loads(proc2.stdout)["all_passed"])
            proc3 = subprocess.run(
                [sys.executable, "-I", str(ROOT / "fw.py"), "payment",
                 "--state", str(state), "--release", "RC", "--payment-id", "P1"],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(proc3.stdout)["payment_id"], "P1")

    def test_cli_unknown_payment_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            build_release(ROOT, state, "RC")
            proc = subprocess.run(
                [sys.executable, "-I", str(ROOT / "fw.py"), "payment",
                 "--state", str(state), "--release", "RC", "--payment-id", "NOPE"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 3)


if __name__ == "__main__":
    unittest.main()
