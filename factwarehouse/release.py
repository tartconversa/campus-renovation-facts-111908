"""分期成形与移交：不可变 release、清单固化、期间差异。

移交语义（对应"固定一期工程移交资料"）：
- 每期 release 落盘为独立目录（warehouse.sqlite + manifest.json + exports/）；
- 已存在的 release 拒绝改写，后续变更只能通过新一期体现，不能悄悄改变旧期解释；
- 新一期 manifest 显式列出更正影响（认可量/价格/验收覆盖重审结果）
  与尚待补证差额（待认可价值、未解释现金、失去认可依据的支付部分）。
"""
from datetime import datetime, timezone
from pathlib import Path
import json

from .conservation import conservation_report
from .facts import build_facts
from .ingest import load_sources
from .pricing import build_timelines
from .storage import build_database, export_flat, open_warehouse
from .validate import hard_errors, validate_sources


def build_release(root, state_dir, release_id, cutoff=None):
    """完整流水线：接收 -> 核对 -> 价格 -> 事实 -> 成形 -> 守恒 -> 导出 -> 清单。"""
    state_dir = Path(state_dir)
    release_dir = state_dir / "releases" / release_id
    if release_dir.exists():
        raise FileExistsError(f"移交期已存在且不可改写：{release_dir}")

    sources = load_sources(root, cutoff=cutoff)
    findings = validate_sources(sources)
    if hard_errors(findings):
        raise ValueError(
            "来源核对存在 error 级问题，拒绝成形："
            + json.dumps(findings, ensure_ascii=False)
        )
    timeline = build_timelines(sources)
    facts = build_facts(sources, timeline)
    if any(w["code"] == "value.unpriced" for w in facts["warnings"]):
        # 无适用价属于待补证，不阻断，但必须进入清单。
        pass

    created_at = datetime.now(timezone.utc).isoformat()
    meta = {
        "release_id": release_id,
        "created_at_utc": created_at,
        "cutoff": cutoff or "full",
        "source_catalog_sha256": sources.catalog["files"][0]["sha256"],
        "source_file_count": str(len(sources.catalog["files"])),
        "warehouse_version": "1.0.0",
    }
    db_path = build_database(
        release_dir / "warehouse.sqlite", sources, facts, timeline, meta
    )
    report = conservation_report(db_path)
    manifest = _manifest(release_id, db_path, cutoff, created_at, findings, facts, report)
    release_dir.mkdir(parents=True, exist_ok=True)
    (release_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    export_flat(db_path, release_dir / "exports")
    return release_dir, manifest


def _manifest(release_id, db_path, cutoff, created_at, findings, facts, report):
    conn = open_warehouse(db_path)
    try:
        totals = {
            "recognized_value_fen": conn.execute(
                "SELECT COALESCE(SUM(value_fen),0) FROM value_f WHERE value_status='recognized'"
            ).fetchone()[0],
            "disputed_claim_value_fen": conn.execute(
                "SELECT COALESCE(SUM(value_fen),0) FROM value_f WHERE value_status='disputed'"
            ).fetchone()[0],
            "pending_value_fen": conn.execute(
                "SELECT COALESCE(SUM(value_fen),0) FROM value_f WHERE value_status='pending'"
            ).fetchone()[0],
            "inactive_basis_value_fen": conn.execute(
                "SELECT COALESCE(SUM(value_fen),0) FROM value_f WHERE value_status='inactive_basis'"
            ).fetchone()[0],
            "lump_non_decomposable_fen": conn.execute(
                "SELECT COALESCE(SUM(total_fen),0) FROM lump_value_f"
            ).fetchone()[0],
            "net_cash_fen": conn.execute(
                "SELECT COALESCE(SUM(amount_fen),0) FROM payment_f"
            ).fetchone()[0],
            "cash_unexplained_fen": conn.execute(
                "SELECT COALESCE(SUM(unexplained_fen),0) FROM payment_f"
            ).fetchone()[0],
            "cash_basis_impaired_fen": conn.execute(
                "SELECT COALESCE(SUM(basis_impaired_fen),0) FROM payment_f"
            ).fetchone()[0],
        }
    finally:
        conn.close()
    return {
        "release_id": release_id,
        "created_at_utc": created_at,
        "cutoff": cutoff or "full",
        "conservation_all_passed": report["all_passed"],
        "conservation_boundaries": report["boundaries"],
        "counts": {name: len(rows) for name, rows in facts.items() if name != "warnings"},
        "totals": totals,
        "validation_warnings": [f for f in findings if f["level"] == "warning"],
        "build_warnings": facts["warnings"],
        "pending_evidence_gap": {
            "pending_value_fen": totals["pending_value_fen"],
            "cash_unexplained_fen": totals["cash_unexplained_fen"],
            "cash_basis_impaired_fen": totals["cash_basis_impaired_fen"],
            "note": "尚待补证的量价差额：不确认收入、不自动退款，仅挂账可查",
        },
    }


def diff_releases(state_dir, old_id, new_id):
    """比较两期移交：更正影响与价值/现金变动，不改动任何一期。"""
    base = Path(state_dir) / "releases"
    old_conn = open_warehouse(base / old_id / "warehouse.sqlite")
    new_conn = open_warehouse(base / new_id / "warehouse.sqlite")
    try:
        old_phys = {r["measure_id"]: dict(r) for r in old_conn.execute("SELECT * FROM physical_measure_f")}
        new_phys = {r["measure_id"]: dict(r) for r in new_conn.execute("SELECT * FROM physical_measure_f")}
        corrections = []
        for mid in sorted(set(old_phys) | set(new_phys)):
            before, after = old_phys.get(mid), new_phys.get(mid)
            if before is None:
                corrections.append({"measure_id": mid, "change": "added", "after": after})
            elif after is None:
                corrections.append({"measure_id": mid, "change": "removed", "before": before})
            elif before["quantity"] != after["quantity"] or before["latest_revision"] != after["latest_revision"]:
                corrections.append({
                    "measure_id": mid,
                    "change": "corrected",
                    "revision": f"{before['latest_revision']} -> {after['latest_revision']}",
                    "quantity": f"{before['quantity']} -> {after['quantity']}",
                })

        old_status = {r["measure_id"]: dict(r) for r in old_conn.execute("SELECT * FROM measure_status_f")}
        new_status = {r["measure_id"]: dict(r) for r in new_conn.execute("SELECT * FROM measure_status_f")}
        coverage_changes = []
        for mid in sorted(set(old_status) | set(new_status)):
            if old_status.get(mid) != new_status.get(mid):
                coverage_changes.append(
                    {"measure_id": mid, "before": old_status.get(mid), "after": new_status.get(mid)}
                )

        def price_rows(conn):
            return {r["timeline_id"]: dict(r) for r in conn.execute("SELECT * FROM price_timeline_f")}
        old_prices, new_prices = price_rows(old_conn), price_rows(new_conn)
        price_changes = [
            {"after": row}
            for key, row in new_prices.items()
            if row not in old_prices.values()
        ]

        def value_totals(conn):
            return {
                r["value_status"]: r["amount"]
                for r in conn.execute(
                    "SELECT value_status, COALESCE(SUM(value_fen),0) AS amount "
                    "FROM value_f GROUP BY value_status"
                )
            }
        old_values, new_values = value_totals(old_conn), value_totals(new_conn)
        value_delta = {
            status: new_values.get(status, 0) - old_values.get(status, 0)
            for status in sorted(set(old_values) | set(new_values))
        }

        old_pay = {r["payment_id"]: dict(r) for r in old_conn.execute("SELECT * FROM payment_f")}
        new_pay = {r["payment_id"]: dict(r) for r in new_conn.execute("SELECT * FROM payment_f")}
        cash_changes = []
        for pid in sorted(set(old_pay) | set(new_pay)):
            if old_pay.get(pid) != new_pay.get(pid):
                cash_changes.append(
                    {"payment_id": pid, "before": old_pay.get(pid), "after": new_pay.get(pid)}
                )
    finally:
        old_conn.close()
        new_conn.close()

    return {
        "from_release": old_id,
        "to_release": new_id,
        "physical_corrections": corrections,
        "coverage_reviews": coverage_changes,
        "price_changes": price_changes,
        "value_delta_fen": value_delta,
        "cash_basis_changes": cash_changes,
        "rule": "更正到来时同时重审认可量、适用价格与验收覆盖；旧期数值不被改写",
    }
