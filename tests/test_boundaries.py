"""四边界守恒与核心业务规则测试。

覆盖：修订替代、超量争议隔离、替代/驳回不增量、追溯价边界、
整栋不可分、现金未解释/冲销/防重复、不可变移交、期间差异。
"""
from pathlib import Path
import tempfile
import unittest

import _path  # noqa: F401  隔离模式路径引导
from factwarehouse.ingest import load_sources
from factwarehouse.pricing import build_timelines, price_for
from factwarehouse.facts import build_facts
from factwarehouse.release import build_release, diff_releases
from factwarehouse.conservation import conservation_report
from factwarehouse.storage import open_warehouse
from tests.factories import (
    build_source_dir, bill, measure, link, variation, payment,
)

ROOT = Path(__file__).resolve().parents[1]


def build_in_tmp(bills, measures, links, variations, payments, cutoff=None):
    tmp = tempfile.TemporaryDirectory()
    self_tmp = tmp

    class Holder:
        pass

    holder = Holder()
    holder.tmp = tmp
    source = build_source_dir(Path(tmp.name) / "src", bills, measures, links, variations, payments)
    state = Path(tmp.name) / "state"
    release_dir, manifest = build_release(source, state, "RX", cutoff=cutoff)
    holder.release_dir = release_dir
    holder.manifest = manifest
    return holder


# 一组干净基线：两个可计价清单版本、三个计量、正常覆盖与一笔支付。
def baseline_bills():
    return [
        bill("KA", "L1", 1, "通道", "m2", "1000", 100000, "2026-10-01"),
        bill("KB", "L2", 1, "活动", "m2", "2000", 200000, "2026-10-01"),
    ]


class PhysicalBoundary(unittest.TestCase):
    def test_revision_replaces_not_adds(self):
        """同计量号修订：实物量按最新修订，旧修订留痕但不增量。"""
        h = build_in_tmp(
            baseline_bills(),
            [
                measure("M1", 1, "KA", "S1", "10", "2026-10-10"),
                measure("M1", 2, "KA", "S1", "8", "2026-10-10"),
            ],
            [link("A1", "AC1", "M1", "8")],
            [], [],
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        rows = conn.execute("SELECT * FROM physical_measure_f").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], "8")
        self.assertEqual(rows[0]["latest_revision"], 2)
        self.assertEqual(rows[0]["superseded_revisions"], "1")
        raw = conn.execute("SELECT COUNT(*) c FROM raw_measure_f").fetchone()["c"]
        self.assertEqual(raw, 2)  # 原始两行全部留痕
        conn.close()

    def test_revision_correction_triggers_dispute_isolation(self):
        """计量更正使认可超量：全部竞争覆盖争议，未争议计量继续可查。"""
        h = build_in_tmp(
            baseline_bills(),
            [
                measure("M1", 1, "KA", "S1", "5", "2026-10-10"),
                measure("M1", 2, "KA", "S1", "3", "2026-10-10"),
                measure("M2", 1, "KB", "S2", "4", "2026-10-11"),
            ],
            [
                link("A1", "AC1", "M1", "2"),
                link("A2", "AC2", "M1", "2"),
                link("A3", "AC1", "M2", "4"),
            ],
            [], [],
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        m1 = conn.execute("SELECT * FROM measure_status_f WHERE measure_id='M1'").fetchone()
        m2 = conn.execute("SELECT * FROM measure_status_f WHERE measure_id='M2'").fetchone()
        self.assertEqual(m1["is_disputed"], 1)
        self.assertEqual(m1["undisputed_quantity"], "0")
        self.assertEqual(m1["disputed_claim_quantity"], "4")
        self.assertEqual(m1["over_coverage_quantity"], "1")
        # 未争议部分独立保留
        self.assertEqual(m2["is_disputed"], 0)
        self.assertEqual(m2["undisputed_quantity"], "4")
        disputed_values = conn.execute(
            "SELECT COUNT(*) c FROM value_f WHERE measure_id='M1' AND value_status='disputed'"
        ).fetchone()["c"]
        recognized_on_m1 = conn.execute(
            "SELECT COUNT(*) c FROM value_f WHERE measure_id='M1' AND value_status='recognized'"
        ).fetchone()["c"]
        self.assertEqual(disputed_values, 2)
        self.assertEqual(recognized_on_m1, 0)
        conn.close()


class AcceptanceBoundary(unittest.TestCase):
    def test_supersede_adds_no_physical_quantity(self):
        """A4 替代 A3：认可不新增实物量，失效依据单列留痕。"""
        h = build_in_tmp(
            baseline_bills(),
            [measure("M1", 1, "KA", "S1", "10", "2026-10-10")],
            [
                link("A3", "AC3", "M1", "5"),
                link("A4", "AC4", "M1", "5", supersedes="A3"),
            ],
            [], [],
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        active = conn.execute(
            "SELECT COALESCE(SUM(CAST(link_quantity AS REAL)),0) q "
            "FROM acceptance_cover_f WHERE is_active=1"
        ).fetchone()["q"]
        self.assertEqual(active, 5)  # 不是 10
        inactive = conn.execute(
            "SELECT value_status, COUNT(*) c FROM value_f GROUP BY value_status"
        ).fetchall()
        statuses = {r["value_status"]: r["c"] for r in inactive}
        self.assertEqual(statuses.get("inactive_basis"), 1)
        self.assertEqual(statuses.get("recognized"), 1)
        conn.close()

    def test_rejected_reinspection_does_not_add_quantity(self):
        """驳回后复验：驳回覆盖不贡献认可，复验认可不超过实物。"""
        h = build_in_tmp(
            baseline_bills(),
            [measure("M1", 1, "KA", "S1", "6", "2026-10-10")],
            [
                link("A1", "AC1", "M1", "6", status="rejected"),
                link("A2", "AC2", "M1", "6"),
            ],
            [], [],
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        row = conn.execute("SELECT * FROM measure_status_f WHERE measure_id='M1'").fetchone()
        self.assertEqual(row["undisputed_quantity"], "6")
        self.assertEqual(row["pending_quantity"], "0")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM value_f WHERE value_status='inactive_basis'"
        ).fetchone()["c"], 1)
        conn.close()

    def test_many_to_many_acceptance_measure(self):
        """一张验收单认可多张计量，一张计量跨多张验收单。"""
        h = build_in_tmp(
            baseline_bills(),
            [
                measure("M1", 1, "KA", "S1", "10", "2026-10-10"),
                measure("M2", 1, "KB", "S2", "8", "2026-10-11"),
            ],
            [
                link("A1", "AC1", "M1", "4"),
                link("A2", "AC2", "M1", "3"),
                link("A3", "AC1", "M2", "5"),
            ],
            [], [],
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        acc1 = conn.execute(
            "SELECT COUNT(*) c FROM acceptance_cover_f WHERE acceptance_id='AC1'"
        ).fetchone()["c"]
        self.assertEqual(acc1, 2)
        m1 = conn.execute(
            "SELECT COUNT(*) c FROM acceptance_cover_f WHERE measure_id='M1' AND is_active=1"
        ).fetchone()["c"]
        self.assertEqual(m1, 2)
        m1_status = conn.execute(
            "SELECT * FROM measure_status_f WHERE measure_id='M1'"
        ).fetchone()
        self.assertEqual(m1_status["undisputed_quantity"], "7")
        self.assertEqual(m1_status["pending_quantity"], "3")
        conn.close()


class ValueBoundary(unittest.TestCase):
    def test_retroactive_price_revalues_only_from_effective_date(self):
        """追溯改价：生效日及之后的事件用新价，更早事件保留旧价。"""
        bills = [bill("KA", "L1", 1, "通道", "m2", "1000", 100000, "2026-10-01")]
        variations = [variation("V1", ["KA"], ["KA"], "2026-10-15", True, "1200")]
        sources = load_sources(
            build_source_dir(
                Path(tempfile.mkdtemp()) / "src", bills,
                [measure("M1", 1, "KA", "S1", "1", "2026-10-14"),
                 measure("M2", 1, "KA", "S1", "1", "2026-10-15")],
                [], variations, [],
            ),
            verify=True,
        )
        timeline = build_timelines(sources)
        old_price, _ = price_for(timeline, "KA", sources.measures[0]["measured_on"])
        new_price, _ = price_for(timeline, "KA", sources.measures[1]["measured_on"])
        self.assertEqual(old_price, 1000)
        self.assertEqual(new_price, 1200)

    def test_scope_only_variation_never_reprices_history(self):
        """纯范围变化（retroactive=false）不写价格段，旧价不变。"""
        bills = [
            bill("K1V1", "L1", 1, "整栋", "", "", 1000000, "2026-10-01"),
            bill("KA", "L1", 2, "通道", "m2", "1000", 480000, "2026-10-15"),
            bill("KB", "L2", 2, "活动", "m2", "2000", 520000, "2026-10-15"),
        ]
        variations = [variation("V1", ["K1V1"], ["KA", "KB"], "2026-10-15", False)]
        h = build_in_tmp(
            bills,
            [measure("M1", 1, "KA", "S1", "10", "2026-10-16")],
            [link("A1", "AC1", "M1", "10")],
            variations, [],
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        # 整栋总价留在不可分解层
        lump = conn.execute("SELECT item_key,total_fen FROM lump_value_f").fetchall()
        self.assertEqual([r["item_key"] for r in lump], ["K1V1"])
        self.assertEqual(lump[0]["total_fen"], 1000000)
        # 空间汇总里没有整栋分摊行
        spaces = conn.execute("SELECT DISTINCT item_key FROM v_space_summary").fetchall()
        self.assertNotIn("K1V1", [r["item_key"] for r in spaces])
        # KA 仍按清单自身单价 1000 计价
        value = conn.execute("SELECT value_fen,price_source FROM value_f").fetchone()
        self.assertEqual(value["value_fen"], 10000)
        # 血缘保留拆项来源
        lineage = conn.execute("SELECT * FROM variation_lineage_f").fetchall()
        self.assertEqual(len(lineage), 2)
        self.assertTrue(all(r["scope_only"] for r in lineage))
        conn.close()

    def test_lump_sum_never_averaged_across_rooms(self):
        """缺分解依据的整栋款：不产生任何逐量价值行。"""
        bills = [bill("K1V1", "L1", 1, "整栋", "", "", 900000, "2026-10-01")]
        h = build_in_tmp(bills, [], [], [], [])
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM value_f").fetchone()["c"], 0)
        self.assertEqual(
            conn.execute("SELECT SUM(total_fen) s FROM lump_value_f").fetchone()["s"], 900000
        )
        conn.close()


class CashBoundary(unittest.TestCase):
    def test_payment_kept_when_basis_disputed_without_auto_refund(self):
        """支付覆盖争议验收：现金保留，失去依据部分显式标记，不自动退款。"""
        h = build_in_tmp(
            baseline_bills(),
            [
                measure("M1", 1, "KA", "S1", "3", "2026-10-10"),
                measure("M2", 1, "KA", "S2", "3", "2026-10-10"),
            ],
            [
                link("A1", "AC1", "M1", "2"),
                link("A2", "AC2", "M1", "2"),
                link("A3", "AC1", "M2", "3"),
            ],
            [],
            [payment("P1", 50000, ["AC1", "AC2"], "2026-10-30")],
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        p1 = conn.execute("SELECT * FROM payment_f").fetchone()
        # 不生成任何额外退款流水
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM payment_f").fetchone()["c"], 1)
        # 依据分量 + 未解释 = 付款额，未解释非负
        self.assertGreaterEqual(p1["unexplained_fen"], 0)
        self.assertEqual(
            p1["recognized_basis_fen"] + p1["disputed_basis_fen"]
            + p1["inactive_basis_fen"] + p1["unexplained_fen"],
            50000,
        )
        self.assertGreater(p1["basis_impaired_fen"], 0)
        conn.close()

    def test_explicit_refund_reverses_scoped_payment_only(self):
        """显式退款有引用范围；同一金额在多对多连接中不重复。"""
        h = build_in_tmp(
            baseline_bills(),
            [measure("M1", 1, "KA", "S1", "5", "2026-10-10")],
            [link("A1", "AC1", "M1", "5")],
            [],
            [
                payment("P1", 50000, ["AC1"], "2026-10-30"),
                payment("P2", -10000, ["AC1"], "2026-11-02", reverses="P1"),
            ],
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        net = conn.execute("SELECT SUM(amount_fen) s FROM payment_f").fetchone()["s"]
        self.assertEqual(net, 40000)
        # 价值事实只有一条；两笔支付各自桥接一次（共 2 条），金额不膨胀
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM value_f").fetchone()["c"], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM cash_basis_bridge_f").fetchone()["c"], 2
        )
        bridge_sum = conn.execute(
            "SELECT SUM(allocated_basis_fen) s FROM cash_basis_bridge_f"
        ).fetchone()["s"]
        component = conn.execute(
            "SELECT SUM(recognized_basis_fen+disputed_basis_fen+inactive_basis_fen) s "
            "FROM payment_f"
        ).fetchone()["s"]
        self.assertEqual(bridge_sum, component)
        p2 = conn.execute("SELECT * FROM payment_f WHERE payment_id='P2'").fetchone()
        self.assertEqual(p2["reverses"], "P1")
        self.assertGreaterEqual(p2["unexplained_fen"], 0)
        conn.close()

    def test_unexplained_difference_is_explicit_not_allocated(self):
        """付款超出声明范围价值：差额显式列示，绝不平均分摊。"""
        h = build_in_tmp(
            baseline_bills(),
            [measure("M1", 1, "KA", "S1", "2", "2026-10-10")],
            [link("A1", "AC1", "M1", "2")],
            [],
            [payment("P1", 9000, ["AC1"], "2026-10-30")],  # 依据仅 2000
        )
        self.addCleanup(h.tmp.cleanup)
        conn = open_warehouse(h.release_dir / "warehouse.sqlite")
        p1 = conn.execute("SELECT * FROM payment_f").fetchone()
        self.assertEqual(p1["recognized_basis_fen"], 2000)
        self.assertEqual(p1["unexplained_fen"], 7000)
        conn.close()


class ReleaseImmutability(unittest.TestCase):
    def test_release_cannot_be_overwritten(self):
        h = build_in_tmp(
            baseline_bills(),
            [measure("M1", 1, "KA", "S1", "2", "2026-10-10")],
            [link("A1", "AC1", "M1", "2")], [], [],
        )
        self.addCleanup(h.tmp.cleanup)
        tmp_root = Path(h.tmp.name)
        with self.assertRaises(FileExistsError):
            build_release(tmp_root / "src", tmp_root / "state", "RX")

    def test_diff_captures_correction_and_pending_gap(self):
        """新一期体现更正影响，旧期不变；差异报告列出量价变动。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "src"
        state = Path(tmp.name) / "state"
        common = dict(
            bills=baseline_bills(),
            links=[link("A1", "AC1", "M1", "5")],
            variations=[], payments=[],
        )
        build_source_dir(src, measures=[measure("M1", 1, "KA", "S1", "5", "2026-10-10")], **common)
        build_release(src, state, "R1")
        # 第二期：计量更正为 3 -> 超量争议
        build_source_dir(src, measures=[
            measure("M1", 1, "KA", "S1", "5", "2026-10-10"),
            measure("M1", 2, "KA", "S1", "3", "2026-10-10"),
        ], **common)
        build_release(src, state, "R2")
        diff = diff_releases(state, "R1", "R2")
        self.assertEqual(diff["physical_corrections"][0]["change"], "corrected")
        self.assertTrue(diff["coverage_reviews"])
        # 旧期不可变：R1 里 M1 仍非争议
        old = open_warehouse(state / "releases" / "R1" / "warehouse.sqlite")
        self.assertEqual(
            old.execute("SELECT is_disputed FROM measure_status_f").fetchone()["is_disputed"], 0
        )
        old.close()

    def test_cutoff_partitions_facts_by_period(self):
        """截止日：日期事实按期过滤，被排除行留痕 included_in_cutoff=0。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "src"
        state = Path(tmp.name) / "state"
        build_source_dir(
            src,
            baseline_bills(),
            [
                measure("M1", 1, "KA", "S1", "2", "2026-10-10"),
                measure("M2", 1, "KB", "S2", "3", "2026-10-20"),
            ],
            [link("A1", "AC1", "M1", "2")], [],
            [payment("P1", 1000, ["AC1"], "2026-10-12")],
        )
        build_release(src, state, "R1", cutoff="2026-10-15")
        conn = open_warehouse(state / "releases" / "R1" / "warehouse.sqlite")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM physical_measure_f").fetchone()["c"], 1
        )
        excluded = conn.execute(
            "SELECT COUNT(*) c FROM raw_measure_f WHERE included_in_cutoff=0"
        ).fetchone()["c"]
        self.assertEqual(excluded, 1)
        conn.close()


class WorkspaceDataConservation(unittest.TestCase):
    """对仓库自带合成资料全量成形，四边界守恒必须全部通过。"""

    def test_full_pipeline_on_repo_data(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / "state"
        release_dir, manifest = build_release(ROOT, state, "R001")
        self.assertTrue(manifest["conservation_all_passed"])
        report = conservation_report(release_dir / "warehouse.sqlite")
        failed = [c for c in report["checks"] if not c["passed"]]
        self.assertEqual(failed, [])
        # 关键数字断言（与业务情景对应）
        totals = manifest["totals"]
        self.assertEqual(totals["recognized_value_fen"], 20000)       # M2 × 2 × 10000
        self.assertEqual(totals["disputed_claim_value_fen"], 25000)   # M1 争议 2 × 12500
        self.assertEqual(totals["lump_non_decomposable_fen"], 1000000)
        self.assertEqual(totals["net_cash_fen"], 40000)               # 50000 - 10000
        self.assertEqual(totals["cash_unexplained_fen"], 25000)
        self.assertEqual(totals["cash_basis_impaired_fen"], 35000)
        # 追溯价生效：K1V2A 适用 12500
        conn = open_warehouse(release_dir / "warehouse.sqlite")
        prices = [
            r["price_fen"]
            for r in conn.execute(
                "SELECT DISTINCT price_fen FROM price_timeline_f WHERE item_key='K1V2A'"
            )
        ]
        self.assertEqual(sorted(prices), [12000, 12500])
        conn.close()


if __name__ == "__main__":
    unittest.main()
