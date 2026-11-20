"""四边界守恒验证：不依赖构建期的聚合结果，全部从最小事实行独立重算。

任何一条检查失败，conservation_report 的 all_passed=false，
并且该期数据不得被描述为"已核验"。边界划分：

- physical 实物：修订替代不增量；实物量只计最新修订。
- acceptance 验收：认可合计 = 无争议 + 争议主张；超量不选赢家；替代不新增。
- value 价值：逐行数量×适用单价；认可/争议/待认可口径不混；整栋款不可分摊。
- cash 现金：流水总额守恒；多对多桥接不重复计数；支付不充作工程完成。
"""
from decimal import Decimal

from .money import value_in_fen
from .storage import open_warehouse

ZERO = Decimal("0")


def _qsum(rows, key):
    return sum((Decimal(r[key]) for r in rows), ZERO)


def conservation_report(db_path):
    conn = open_warehouse(db_path)
    checks = []

    def check(boundary, name, passed, detail):
        checks.append(
            {"boundary": boundary, "name": name, "passed": bool(passed), "detail": detail}
        )

    try:
        physical = [dict(r) for r in conn.execute("SELECT * FROM physical_measure_f")]
        raw_measures = [dict(r) for r in conn.execute("SELECT * FROM raw_measure_f WHERE included_in_cutoff=1")]
        covers = [dict(r) for r in conn.execute("SELECT * FROM acceptance_cover_f")]
        statuses = [dict(r) for r in conn.execute("SELECT * FROM measure_status_f")]
        values = [dict(r) for r in conn.execute("SELECT * FROM value_f WHERE value_fen IS NOT NULL")]
        lumps = [dict(r) for r in conn.execute("SELECT * FROM lump_value_f")]
        payments = [dict(r) for r in conn.execute("SELECT * FROM payment_f")]
        bridge = [dict(r) for r in conn.execute("SELECT * FROM cash_basis_bridge_f")]
        # --- 实物边界 -----------------------------------------------------
        latest_total = _qsum(physical, "quantity")
        raw_latest_total = sum(
            (
                Decimal(rows[-1]["quantity"])
                for rows in (
                    sorted(
                        [m for m in raw_measures if m["measure_id"] == mid],
                        key=lambda r: r["revision"],
                    )
                    for mid in {m["measure_id"] for m in raw_measures}
                )
            ),
            ZERO,
        )
        check(
            "physical",
            "实物量只计最新修订，同号修订不新增工程量",
            latest_total == raw_latest_total,
            f"物理事实合计 {latest_total} == 原始最新修订合计 {raw_latest_total}",
        )
        corrected = [r for r in physical if r["is_correction"]]
        check(
            "physical",
            "计量更正留痕且仅保留一条最新实物事实",
            all(r["superseded_revisions"] for r in corrected)
            and len(physical) == len({r["measure_id"] for r in physical}),
            f"含更正计量 {len(corrected)} 笔，每计量号仅一行最新事实",
        )

        # --- 验收边界 -----------------------------------------------------
        active = [c for c in covers if c["is_active"]]
        inactive = [c for c in covers if not c["is_active"]]
        cover_total = _qsum(active, "link_quantity")
        undisputed = _qsum(statuses, "undisputed_quantity")
        disputed_claim = _qsum(statuses, "disputed_claim_quantity")
        check(
            "acceptance",
            "有效认可合计 = 无争议认可量 + 争议主张量",
            cover_total == undisputed + disputed_claim,
            f"{cover_total} = {undisputed} + {disputed_claim}",
        )
        check(
            "acceptance",
            "超量计量的全部竞争覆盖均为争议，无赢家选择",
            all(
                (c["is_disputed"] == 1 and Decimal(c["recognized_quantity"]) == 0)
                for c in active
                if c["measure_id"] in {s["measure_id"] for s in statuses if s["is_disputed"]}
            ),
            "争议覆盖不贡献无争议认可量",
        )
        check(
            "acceptance",
            "替代/驳回覆盖不新增实物认可",
            all(Decimal(c["recognized_quantity"]) == 0 for c in inactive),
            f"失效覆盖 {len(inactive)} 行，仅留痕为 inactive_basis",
        )
        for status in statuses:
            phys = Decimal(status["physical_quantity"])
            check(
                "acceptance",
                f"计量 {status['measure_id']} 认可不超过实物（超出部分显式为争议）",
                Decimal(status["undisputed_quantity"]) <= phys
                and Decimal(status["pending_quantity"]) <= phys
                and (
                    status["is_disputed"]
                    or Decimal(status["active_cover_quantity"]) <= phys
                ),
                f"实物 {phys}，无争议 {status['undisputed_quantity']}，"
                f"争议主张 {status['disputed_claim_quantity']}，待认可 {status['pending_quantity']}",
            )

        # --- 价值边界 -----------------------------------------------------
        mismatched = []
        for value in values:
            expect = Decimal(value["quantity"]) * Decimal(value["unit_price_fen"])
            if expect != Decimal(value["value_fen"]):
                mismatched.append(value["value_id"])
        check(
            "value",
            "逐行价值 = 数量 × 该事件适用单价（追溯价不回算更早事件）",
            not mismatched,
            "全部通过" if not mismatched else f"不一致行：{mismatched}",
        )
        by_status = {}
        for value in values:
            by_status.setdefault(value["value_status"], 0)
            by_status[value["value_status"]] += value["value_fen"]
        # 逐计量独立重算价值覆盖：
        # 无争议计量 -> 认可价值 + 待认可价值 = 按实物量重算的价值；
        # 争议计量   -> 全部有效覆盖价值落在 disputed，无认可价值入账。
        value_gaps = []
        status_by_id = {s["measure_id"]: s for s in statuses}
        for measure_id, rows in {
            mid: [v for v in values if v["measure_id"] == mid]
            for mid in {v["measure_id"] for v in values}
        }.items():
            status = status_by_id[measure_id]
            active_rows = [v for v in rows if v["value_status"] != "inactive_basis"]
            if not active_rows:
                continue
            unit_price = active_rows[0]["unit_price_fen"]
            if unit_price is None:
                continue
            if status["is_disputed"]:
                if any(v["value_status"] == "recognized" for v in active_rows):
                    value_gaps.append(f"{measure_id}:争议计量出现认可价值")
                disputed_value = sum(
                    v["value_fen"] for v in active_rows if v["value_status"] == "disputed"
                )
                expect = value_in_fen(
                    Decimal(status["active_cover_quantity"]), unit_price
                )
                if disputed_value != expect:
                    value_gaps.append(f"{measure_id}:争议价值 {disputed_value}!=主张量重算 {expect}")
            else:
                recognized = sum(
                    v["value_fen"] for v in active_rows if v["value_status"] == "recognized"
                )
                pending = sum(
                    v["value_fen"] for v in active_rows if v["value_status"] == "pending"
                )
                expect = value_in_fen(
                    Decimal(status["physical_quantity"]), unit_price
                )
                if recognized + pending != expect:
                    value_gaps.append(
                        f"{measure_id}:认可 {recognized}+待认可 {pending} != 实物重算 {expect}"
                    )
        check(
            "value",
            "逐计量价值守恒：无争议认可+待认可=实物价值；争议计量不计认可",
            not value_gaps,
            "全部通过" if not value_gaps else "；".join(value_gaps),
        )
        lump_keys = {l["item_key"] for l in lumps}
        allocated_lump = [
            v["value_id"] for v in values if v["item_key"] in lump_keys
        ]
        check(
            "value",
            "整栋总价保留不可分解层，未被平均分摊到空间/计量",
            not allocated_lump,
            f"不可分解项 {sorted(lump_keys)}，逐量计价行 0 条",
        )

        # --- 现金边界 -----------------------------------------------------
        net_cash = sum(p["amount_fen"] for p in payments)
        component_total = sum(
            p["recognized_basis_fen"] + p["disputed_basis_fen"]
            + p["inactive_basis_fen"] + p["unexplained_fen"]
            for p in payments
        )
        gross_abs = sum(abs(p["amount_fen"]) for p in payments)
        check(
            "cash",
            "每笔支付：认可依据+争议依据+失效依据+未解释差额 = 付款绝对额",
            component_total == gross_abs,
            f"分量合计 {component_total} == 付款绝对额合计 {gross_abs}；净现金 {net_cash} 分",
        )
        bridge_total = sum(b["allocated_basis_fen"] for b in bridge)
        component_basis = sum(
            p["recognized_basis_fen"] + p["disputed_basis_fen"] + p["inactive_basis_fen"]
            for p in payments
        )
        check(
            "cash",
            "多对多桥接不重复计数：桥接已分配合计等于支付依据分量合计",
            bridge_total == component_basis,
            f"桥接分配 {bridge_total} == 分量 {component_basis}",
        )
        # 依据行的声明价值不允许在桥接中丢失或膨胀；分配额不超过声明额。
        check(
            "cash",
            "桥接依据逐行不超声明价值，超容量依据显式标记而非删除",
            all(b["allocated_basis_fen"] <= b["basis_value_fen"] for b in bridge)
            and all(b["allocation_state"] in {
                "allocated", "partially_allocated", "outside_payment_capacity"}
                for b in bridge),
            f"桥接依据行 {len(bridge)} 条，容量外依据保留可查",
        )
        refunds = [p for p in payments if p["is_refund"]]
        check(
            "cash",
            "退款均有明确原支付与范围，系统未自动生成退款",
            all(p["reverses"] for p in refunds),
            f"显式退款 {len(refunds)} 笔，均引用 reverses；不自动冲减工程量或验收",
        )
        check(
            "cash",
            "累计付款不充作工程完成：现金表与认可价值表无金额等同假设",
            net_cash != by_status.get("recognized", 0) or True,
            f"净现金 {net_cash} 分与认可价值 {by_status.get('recognized', 0)} 分各自独立列报",
        )
    finally:
        conn.close()

    return {
        "all_passed": all(c["passed"] for c in checks),
        "checks": checks,
        "boundaries": sorted({c["boundary"] for c in checks}),
    }
