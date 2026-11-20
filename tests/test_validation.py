"""来源核对负向测试：error 阻断成形，warning 不阻断。"""
from pathlib import Path
import tempfile
import unittest

import _path  # noqa: F401  隔离模式路径引导
from factwarehouse.ingest import load_sources
from factwarehouse.validate import validate_sources, hard_errors
from factwarehouse.release import build_release
from tests.factories import (
    build_source_dir, bill, measure, link, variation, payment,
)


def bills():
    return [
        bill("KA", "L1", 1, "通道", "m2", "1000", 100000, "2026-10-01"),
    ]


def measures():
    return [measure("M1", 1, "KA", "S1", "5", "2026-10-10")]


def links():
    return [link("A1", "AC1", "M1", "5")]


class ValidationErrors(unittest.TestCase):
    def _findings(self, **overrides):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        payload = dict(bills=bills(), measures=measures(), links=links(), variations=[], payments=[])
        payload.update(overrides)
        source = build_source_dir(Path(tmp.name) / "src", **payload)
        return validate_sources(load_sources(source))

    def test_measure_revision_duplicate(self):
        findings = self._findings(measures=[
            measure("M1", 1, "KA", "S1", "5", "2026-10-10"),
            measure("M1", 1, "KA", "S1", "4", "2026-10-10"),
        ])
        self.assertTrue(any(f["code"] == "measure.duplicate_revision" for f in hard_errors(findings)))

    def test_measure_unknown_item(self):
        findings = self._findings(measures=[measure("M1", 1, "NOPE", "S1", "5", "2026-10-10")])
        self.assertTrue(any(f["code"] == "measure.unknown_item" for f in hard_errors(findings)))

    def test_revision_cannot_change_anchor(self):
        findings = self._findings(measures=[
            measure("M1", 1, "KA", "S1", "5", "2026-10-10"),
            measure("M1", 2, "KA", "S9", "4", "2026-10-10"),
        ])
        self.assertTrue(
            any(f["code"] == "measure.revision_changed_anchor" for f in hard_errors(findings))
        )

    def test_cross_measure_supersede_rejected(self):
        findings = self._findings(
            measures=[
                measure("M1", 1, "KA", "S1", "5", "2026-10-10"),
                measure("M2", 1, "KA", "S2", "5", "2026-10-11"),
            ],
            links=[
                link("A1", "AC1", "M1", "5"),
                link("A2", "AC2", "M2", "5", supersedes="A1"),
            ],
        )
        self.assertTrue(
            any(f["code"] == "link.cross_measure_supersede" for f in hard_errors(findings))
        )

    def test_retroactive_without_price_rejected(self):
        findings = self._findings(
            variations=[variation("V1", ["KA"], ["KA"], "2026-10-15", True)]
        )
        self.assertTrue(
            any(f["code"] == "variation.retro_without_price" for f in hard_errors(findings))
        )

    def test_refund_without_reverses_rejected(self):
        findings = self._findings(
            payments=[payment("P1", -100, ["AC1"], "2026-10-30")]
        )
        self.assertTrue(
            any(f["code"] == "payment.refund_without_reverses" for f in hard_errors(findings))
        )

    def test_chain_reversal_rejected(self):
        findings = self._findings(
            payments=[
                payment("P1", 1000, ["AC1"], "2026-10-30"),
                payment("P2", -100, ["AC1"], "2026-10-31", reverses="P1"),
                payment("P3", -50, ["AC1"], "2026-11-01", reverses="P2"),
            ]
        )
        self.assertTrue(
            any(f["code"] == "payment.chain_reversal" for f in hard_errors(findings))
        )

    def test_hard_error_blocks_release(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = build_source_dir(
            Path(tmp.name) / "src",
            bills(),
            [measure("M1", 1, "GHOST", "S1", "5", "2026-10-10")],
            links(), [], [],
        )
        with self.assertRaises(ValueError):
            build_release(source, Path(tmp.name) / "state", "RX")


class ValidationWarnings(unittest.TestCase):
    def test_line_reuse_is_warning_not_error(self):
        """编号跨版本重用是业务事实，警告提示按 item_key 解释，不阻断。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = build_source_dir(
            Path(tmp.name) / "src",
            [
                bill("KA", "L1", 1, "通道旧", "m2", "1000", 100000, "2026-10-01", "2026-10-15"),
                bill("KB", "L1", 2, "通道新", "m2", "1100", 110000, "2026-10-15"),
            ],
            [measure("M1", 1, "KB", "S1", "5", "2026-10-16")],
            [link("A1", "AC1", "M1", "5")], [], [],
        )
        findings = validate_sources(load_sources(source))
        self.assertFalse(hard_errors(findings))
        self.assertTrue(any(f["code"] == "bill.line_reused" for f in findings))
        # 不阻断成形
        release_dir, manifest = build_release(source, Path(tmp.name) / "state", "RX")
        self.assertTrue(manifest["conservation_all_passed"])

    def test_over_coverage_is_not_an_input_error(self):
        """超量覆盖是事实层争议状态，不是来源错误。"""
        findings = validate_sources(load_sources(
            build_source_dir(
                Path(tempfile.mkdtemp()) / "src", bills(),
                [measure("M1", 1, "KA", "S1", "3", "2026-10-10")],
                [link("A1", "AC1", "M1", "2"), link("A2", "AC2", "M1", "2")], [], [],
            )
        ))
        self.assertFalse(hard_errors(findings))


if __name__ == "__main__":
    unittest.main()
