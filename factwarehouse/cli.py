"""命令行入口：接收、核对、分期成形、守恒验证、导出、钻取查询。

统一用 -I 隔离模式运行（见 README），例如：
    python -I -m factwarehouse build --release R001
"""
import argparse
import json
import sys
from pathlib import Path

from .conservation import conservation_report
from .ingest import load_sources
from .money import to_jsonable
from .query import (
    acceptance_report,
    drill_measure,
    drill_payment,
    drill_value,
    item_report,
    overview,
    space_report,
)
from .release import build_release, diff_releases
from .storage import export_flat
from .validate import validate_sources

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "state"


def _print(payload):
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=to_jsonable)
    sys.stdout.write("\n")


def _db_for(state, release):
    return Path(state) / "releases" / release / "warehouse.sqlite"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="factwarehouse",
        description="改造工程量价事实仓：实物/验收/价值/现金四边界守恒",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="接收并核验来源资料，输出解析结果与指纹")
    p_ingest.add_argument("--root", default=str(ROOT))
    p_ingest.add_argument("--cutoff", default=None, help="ISO 截止日，用于分期")

    p_validate = sub.add_parser("validate", help="来源相互核对，输出 error/warning 发现")
    p_validate.add_argument("--root", default=str(ROOT))
    p_validate.add_argument("--cutoff", default=None)

    p_build = sub.add_parser("build", help="成形一期不可变事实仓并跑守恒验证")
    p_build.add_argument("--root", default=str(ROOT))
    p_build.add_argument("--state", default=str(DEFAULT_STATE))
    p_build.add_argument("--release", required=True)
    p_build.add_argument("--cutoff", default=None)

    p_verify = sub.add_parser("verify", help="对已成形的一期独立重算四边界守恒")
    p_verify.add_argument("--state", default=str(DEFAULT_STATE))
    p_verify.add_argument("--release", required=True)

    p_export = sub.add_parser("export", help="导出 CSV/JSON 平面文件")
    p_export.add_argument("--state", default=str(DEFAULT_STATE))
    p_export.add_argument("--release", required=True)
    p_export.add_argument("--out", default=None)

    p_diff = sub.add_parser("diff", help="比较两期移交：更正影响与量价差额")
    p_diff.add_argument("--state", default=str(DEFAULT_STATE))
    p_diff.add_argument("--from", dest="old", required=True)
    p_diff.add_argument("--to", dest="new", required=True)

    p_overview = sub.add_parser("overview", help="四边界总览")
    p_overview.add_argument("--state", default=str(DEFAULT_STATE))
    p_overview.add_argument("--release", required=True)

    p_space = sub.add_parser("spaces", help="空间粒度钻取（不含整栋分摊）")
    p_space.add_argument("--state", default=str(DEFAULT_STATE))
    p_space.add_argument("--release", required=True)

    p_item = sub.add_parser("items", help="清单版本粒度与变更血缘")
    p_item.add_argument("--state", default=str(DEFAULT_STATE))
    p_item.add_argument("--release", required=True)

    p_acc = sub.add_parser("acceptance", help="验收单与覆盖关系")
    p_acc.add_argument("--state", default=str(DEFAULT_STATE))
    p_acc.add_argument("--release", required=True)

    p_value = sub.add_parser("value", help="金额 -> 覆盖 -> 计量 -> 清单版本钻取")
    p_value.add_argument("--state", default=str(DEFAULT_STATE))
    p_value.add_argument("--release", required=True)
    p_value.add_argument("--value-id", default=None)
    p_value.add_argument("--acceptance-id", default=None)

    p_measure = sub.add_parser("measure", help="单计量完整画像（修订/覆盖/价值/争议）")
    p_measure.add_argument("--state", default=str(DEFAULT_STATE))
    p_measure.add_argument("--release", required=True)
    p_measure.add_argument("--measure-id", required=True)

    p_pay = sub.add_parser("payment", help="支付 -> 已覆盖依据/未解释/冲销链")
    p_pay.add_argument("--state", default=str(DEFAULT_STATE))
    p_pay.add_argument("--release", required=True)
    p_pay.add_argument("--payment-id", required=True)

    args = parser.parse_args(argv)

    if args.command == "ingest":
        sources = load_sources(args.root, cutoff=args.cutoff)
        _print({
            "cutoff": args.cutoff or "full",
            "files": [e["path"] for e in sources.catalog["files"]],
            "counts": {name: len(rows) for name, rows in sources.all_bundles().items()},
            "excluded_by_cutoff": {
                name: sum(1 for row in rows if row.get("included_in_cutoff") is False)
                for name, rows in sources.all_bundles().items()
            },
        })
        return 0

    if args.command == "validate":
        sources = load_sources(args.root, cutoff=args.cutoff)
        findings = validate_sources(sources)
        _print({"findings": findings, "error_count": sum(f["level"] == "error" for f in findings)})
        return 1 if any(f["level"] == "error" for f in findings) else 0

    if args.command == "build":
        release_dir, manifest = build_release(
            args.root, args.state, args.release, cutoff=args.cutoff
        )
        _print({"release_dir": str(release_dir), "manifest": manifest})
        return 0 if manifest["conservation_all_passed"] else 2

    if args.command == "diff":
        _print(diff_releases(args.state, args.old, args.new))
        return 0

    db_path = _db_for(args.state, args.release)
    if args.command == "verify":
        _print(conservation_report(db_path))
        return 0
    elif args.command == "export":
        out = Path(args.out) if args.out else Path(args.state) / "releases" / args.release / "exports"
        _print({"exported_to": str(export_flat(db_path, out))})
    elif args.command == "overview":
        _print(overview(db_path))
    elif args.command == "spaces":
        _print(space_report(db_path))
    elif args.command == "items":
        _print(item_report(db_path))
    elif args.command == "acceptance":
        _print(acceptance_report(db_path))
    elif args.command == "value":
        _print(drill_value(db_path, args.value_id, args.acceptance_id))
    elif args.command == "measure":
        _print(drill_measure(db_path, args.measure_id))
    elif args.command == "payment":
        payload = drill_payment(db_path, args.payment_id)
        if payload is None:
            _print({"error": f"支付不存在：{args.payment_id}"})
            return 3
        _print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
