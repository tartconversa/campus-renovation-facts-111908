"""事实构建层：实物、验收、价值、不可分解、现金各自成表，边界不混。

所有事实以"可钻取的最小行"落表，聚合只能在查询层对这些行求和，
禁止为方便报表而预连接成宽表——多对多连接会让金额重复出现。

关键判定：
- 计量同号修订取最新修订，旧修订留痕但不贡献实物量；
- 被替代(supersedes)/驳回(rejected)的覆盖不是认可，单列为 inactive_basis；
- 认可合计超过最新实物量时，全部竞争覆盖标记 disputed，不选赢家；
  未争议计量的事实独立保留、继续可查；
- 价值按各事件自身日期解析的适用单价计算，追溯价不重算更早事件；
- 现金支付按其声明的验收范围解析价值依据，超出依据的部分记未解释差额，
  退款只冲明确原支付，系统不生成任何不存在的退款。
"""
from decimal import Decimal

from .money import value_in_fen
from .pricing import price_for

ZERO = Decimal("0")


def _latest_revisions(measures):
    """同计量号取最新修订；返回 {measure_id: 最新行} 与留痕信息。"""
    grouped = {}
    for row in measures:
        if not row.get("included_in_cutoff", True):
            continue
        grouped.setdefault(row["measure_id"], []).append(row)
    latest = {}
    for measure_id, rows in grouped.items():
        rows = sorted(rows, key=lambda r: r["revision"])
        top = rows[-1]
        latest[measure_id] = {
            **top,
            "superseded_revisions": [r["revision"] for r in rows[:-1]],
            "is_correction": len(rows) > 1,
        }
    return latest


def build_facts(sources, timeline):
    warnings = []
    physical = _latest_revisions(sources.measures)

    superseded_ids = {
        link["supersedes"]
        for link in sources.acceptance_links
        if link["supersedes"] is not None
    }

    # ---- 验收覆盖事实：逐覆盖行，状态显式，不做单据间取舍 ----------------
    cover_facts = []
    links_by_measure = {}
    for link in sources.acceptance_links:
        is_active = link["status"] == "accepted" and link["link_id"] not in superseded_ids
        row = {
            "link_id": link["link_id"],
            "acceptance_id": link["acceptance_id"],
            "measure_id": link["measure_id"],
            "link_quantity": str(link["quantity"]),
            "status": link["status"],
            "supersedes": link["supersedes"],
            "is_active": int(is_active),
        }
        cover_facts.append(row)
        if is_active and link["measure_id"] in physical:
            links_by_measure.setdefault(link["measure_id"], []).append(
                {**link, "row_ref": row}
            )

    # ---- 逐计量状态：超量则全部争议，未争议部分独立保留 ------------------
    measure_status = {}
    disputed_measures = set()
    for measure_id, phys in physical.items():
        active = links_by_measure.get(measure_id, [])
        cover_total = sum((link["quantity"] for link in active), ZERO)
        phys_qty = phys["quantity"]
        over = cover_total - phys_qty
        is_disputed = over > 0
        if is_disputed:
            disputed_measures.add(measure_id)
            warnings.append(
                {
                    "code": "coverage.over",
                    "message": (
                        f"计量 {measure_id} 有效认可合计 {cover_total} 超过最新实物量 {phys_qty}，"
                        f"超额 {over}：全部竞争覆盖标记争议，不按单据先后选赢家"
                    ),
                    "ref": measure_id,
                }
            )
        measure_status[measure_id] = {
            "measure_id": measure_id,
            "item_key": phys["item_key"],
            "space_id": phys["space_id"],
            "physical_quantity": str(phys_qty),
            "active_cover_quantity": str(cover_total),
            "undisputed_quantity": str(ZERO if is_disputed else cover_total),
            "disputed_claim_quantity": str(cover_total if is_disputed else ZERO),
            "over_coverage_quantity": str(over if is_disputed else ZERO),
            "pending_quantity": str(ZERO if is_disputed else max(phys_qty - cover_total, ZERO)),
            "is_disputed": int(is_disputed),
        }
        for link in active:
            link["row_ref"]["is_disputed"] = int(is_disputed)
            link["row_ref"]["recognized_quantity"] = str(
                ZERO if is_disputed else link["quantity"]
            )
            link["row_ref"]["disputed_quantity"] = str(
                link["quantity"] if is_disputed else ZERO
            )
    for row in cover_facts:
        row.setdefault("is_disputed", 0)
        row.setdefault("recognized_quantity", str(ZERO))
        row.setdefault("disputed_quantity", str(ZERO))

    # ---- 价值事实：认可(无争议)/争议/待认可/失效依据，逐行可计价 ----------
    value_facts = []

    def _price(measure_id):
        phys = physical[measure_id]
        price, price_source = price_for(timeline, phys["item_key"], phys["measured_on"])
        if price is None:
            warnings.append(
                {"code": "value.unpriced", "message": f"{measure_id}：{price_source}", "ref": measure_id}
            )
        return price, price_source

    def _value_row(measure_id, link, quantity, value_status):
        phys = physical[measure_id]
        price, price_source = _price(measure_id)
        return {
            "value_id": (
                f"VAL-{link['link_id']}" if link else f"VAL-PEND-{measure_id}"
            )
            if value_status != "inactive_basis"
            else f"VAL-INACT-{link['link_id']}",
            "measure_id": measure_id,
            "link_id": link["link_id"] if link else None,
            "acceptance_id": link["acceptance_id"] if link else None,
            "item_key": phys["item_key"],
            "space_id": phys["space_id"],
            "quantity": str(quantity),
            "unit_price_fen": price,
            "price_source": price_source,
            "value_fen": value_in_fen(quantity, price) if price is not None else None,
            "value_status": value_status,
            "measured_on": phys["measured_on"].isoformat(),
        }

    for measure_id in physical:
        status = measure_status[measure_id]
        # 有效覆盖：无争议认可 或 争议主张
        for link in links_by_measure.get(measure_id, []):
            if status["is_disputed"]:
                value_facts.append(_value_row(measure_id, link, link["quantity"], "disputed"))
            else:
                value_facts.append(_value_row(measure_id, link, link["quantity"], "recognized"))
        # 待认可（实物量中尚无有效覆盖的部分）
        pending = Decimal(status["pending_quantity"])
        if pending > 0:
            value_facts.append(_value_row(measure_id, None, pending, "pending"))
    # 失效依据：被替代/被驳回的覆盖仍计价留痕，仅供现金解释，不计入认可价值
    for link in sources.acceptance_links:
        is_active = link["status"] == "accepted" and link["link_id"] not in superseded_ids
        if not is_active and link["measure_id"] in physical:
            value_facts.append(_value_row(link["measure_id"], link, link["quantity"], "inactive_basis"))

    # ---- 不可分解层与合同口径总额：禁止平均分摊 --------------------------
    lump_facts = [
        {
            "item_key": bill["item_key"],
            "contract_id": bill["contract_id"],
            "line_no": bill["line_no"],
            "version": bill["version"],
            "total_fen": bill["total_fen"],
            "reason": "无单价整栋总价，缺少分解依据，保留在不可分解层，不分摊到房间",
        }
        for bill in sources.bill_items
        if bill["unit_price_fen"] is None
    ]
    bill_totals = [
        {
            "item_key": bill["item_key"],
            "contract_id": bill["contract_id"],
            "version": bill["version"],
            "unit_price_fen": bill["unit_price_fen"],
            "total_fen": bill["total_fen"],
            "price_from": bill["price_from"].isoformat(),
            "price_until": bill["price_until"].isoformat() if bill["price_until"] else None,
        }
        for bill in sources.bill_items
    ]

    # ---- 变更血缘：拆项价值来源可追，范围变化不回改旧价 ------------------
    lineage = []
    for variation in sources.variations:
        if not variation.get("included_in_cutoff", True):
            continue
        for from_item in variation["from_items"]:
            for to_item in variation["to_items"]:
                lineage.append(
                    {
                        "variation_id": variation["variation_id"],
                        "from_item": from_item,
                        "to_item": to_item,
                        "effective_date": variation["effective_date"].isoformat(),
                        "retroactive": int(variation["retroactive"]),
                        "new_unit_price_fen": variation["unit_price_fen"],
                        "scope_only": int(not variation["retroactive"]),
                    }
                )

    # ---- 现金事实：流水、声明范围、依据桥接、未解释差额 ------------------
    # 分配规则（正付与退款同口径，按金额绝对值）：
    # 一笔支付的依据价值以其自身金额为上限，按 认可 -> 争议 -> 失效依据
    # 的顺序逐行分配；分配不进去的依据行仍保留在桥接表并标记 allocated=0，
    # 未解释差额 = 付款绝对额 - 已分配依据，恒为非负，禁止出现负差额轧账。
    payment_facts = []
    range_facts = []
    basis_bridge = []
    value_by_acceptance = {}
    for value in value_facts:
        if value["acceptance_id"] is None or value["value_fen"] is None:
            continue
        value_by_acceptance.setdefault(value["acceptance_id"], []).append(value)
    status_order = {"recognized": 0, "disputed": 1, "inactive_basis": 2}

    for payment in sources.payments:
        if not payment.get("included_in_cutoff", True):
            continue
        amount_abs = abs(payment["amount_fen"])
        payment_facts.append(
            {
                "payment_id": payment["payment_id"],
                "amount_fen": payment["amount_fen"],
                "occurred_on": payment["occurred_on"].isoformat(),
                "reverses": payment["reverses"],
                "is_refund": int(payment["amount_fen"] < 0),
            }
        )
        for acceptance_id in payment["acceptance_ids"]:
            range_facts.append(
                {"payment_id": payment["payment_id"], "acceptance_id": acceptance_id}
            )

        candidates = []
        for acceptance_id in payment["acceptance_ids"]:
            for value in value_by_acceptance.get(acceptance_id, []):
                candidates.append((acceptance_id, value))
        candidates.sort(key=lambda pair: status_order[pair[1]["value_status"]])

        component_sum = {"recognized": 0, "disputed": 0, "inactive_basis": 0}
        remaining = amount_abs
        for acceptance_id, value in candidates:
            basis_value = value["value_fen"]
            allocated = min(basis_value, remaining)
            basis_bridge.append(
                {
                    "payment_id": payment["payment_id"],
                    "acceptance_id": acceptance_id,
                    "value_id": value["value_id"],
                    "value_status": value["value_status"],
                    "basis_value_fen": basis_value,
                    "allocated_basis_fen": allocated,
                    "allocation_state": (
                        "allocated" if allocated == basis_value
                        else "partially_allocated" if allocated > 0
                        else "outside_payment_capacity"
                    ),
                }
            )
            component_sum[value["value_status"]] += allocated
            remaining -= allocated
        claimed = sum(component_sum.values())
        unexplained = amount_abs - claimed
        if remaining > 0:
            warnings.append(
                {
                    "code": "cash.unexplained",
                    "message": (
                        f"支付 {payment['payment_id']} 有 {remaining} 分缺少认可依据，"
                        "列未解释差额，禁止平均分摊或自动生成退款"
                    ),
                    "ref": payment["payment_id"],
                }
            )
        payment_facts[-1].update(
            {
                "recognized_basis_fen": component_sum["recognized"],
                "disputed_basis_fen": component_sum["disputed"],
                "inactive_basis_fen": component_sum["inactive_basis"],
                "claimed_basis_fen": claimed,
                "unexplained_fen": unexplained,
                "basis_impaired_fen": component_sum["disputed"] + component_sum["inactive_basis"],
            }
        )

    return {
        "warnings": warnings,
        "physical_measure_f": [
            {
                "measure_id": measure_id,
                "item_key": phys["item_key"],
                "space_id": phys["space_id"],
                "latest_revision": phys["revision"],
                "quantity": str(phys["quantity"]),
                "measured_on": phys["measured_on"].isoformat(),
                "superseded_revisions": ",".join(str(r) for r in phys["superseded_revisions"]),
                "is_correction": phys["is_correction"],
            }
            for measure_id, phys in sorted(physical.items())
        ],
        "acceptance_cover_f": sorted(cover_facts, key=lambda r: r["link_id"]),
        "measure_status_f": [measure_status[k] for k in sorted(measure_status)],
        "value_f": sorted(value_facts, key=lambda r: r["value_id"]),
        "lump_value_f": lump_facts,
        "bill_total_f": bill_totals,
        "variation_lineage_f": lineage,
        "payment_f": payment_facts,
        "payment_range_f": range_facts,
        "cash_basis_bridge_f": basis_bridge,
    }
