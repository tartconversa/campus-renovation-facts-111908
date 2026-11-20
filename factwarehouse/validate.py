"""来源相互核对：结构之外的业务引用与规则校验。

ingest 负责"能不能读"，本模块负责"业务上自不自洽"。
error 级发现会阻止数据集成形；warning 级只报告、不阻断。
注意：超量覆盖不是输入错误，是后续事实层要显式呈现的争议状态，
因此本模块不把超量判为 error。
"""


def validate_sources(sources):
    findings = []

    def error(code, message, ref=None):
        findings.append({"level": "error", "code": code, "message": message, "ref": ref})

    def warning(code, message, ref=None):
        findings.append({"level": "warning", "code": code, "message": message, "ref": ref})

    bills = sources.bill_items
    item_keys = {b["item_key"] for b in bills}

    # 清单：同一 item_key 只能有一行；同合同内行号允许跨版本重用（编号修订重用）。
    seen_keys = set()
    line_versions = {}
    for bill in bills:
        if bill["item_key"] in seen_keys:
            error("bill.duplicate_key", f"清单版本行键重复：{bill['item_key']}", bill["item_key"])
        seen_keys.add(bill["item_key"])
        line_versions.setdefault((bill["contract_id"], bill["line_no"]), []).append(
            bill["version"]
        )
    for (contract, line), versions in line_versions.items():
        if len(versions) > 1:
            warning(
                "bill.line_reused",
                f"合同 {contract} 行号 {line} 跨版本重用：{sorted(versions)}，按 item_key 解释版本",
                f"{contract}/{line}",
            )

    # 计量：修订号不得重复；引用的清单版本必须存在；同号修订不得改清单版本/空间。
    revisions = {}
    for measure in sources.measures:
        key = measure["measure_id"]
        revisions.setdefault(key, []).append(measure)
        if measure["item_key"] not in item_keys:
            error(
                "measure.unknown_item",
                f"计量 {key} 引用不存在的清单版本 {measure['item_key']}",
                key,
            )
    for measure_id, rows in revisions.items():
        rev_nos = [r["revision"] for r in rows]
        if len(rev_nos) != len(set(rev_nos)):
            error("measure.duplicate_revision", f"计量 {measure_id} 修订号重复", measure_id)
        anchor = rows[0]
        for row in rows[1:]:
            if row["item_key"] != anchor["item_key"] or row["space_id"] != anchor["space_id"]:
                error(
                    "measure.revision_changed_anchor",
                    f"计量 {measure_id} 修订改变了清单版本或空间，修订只能更正数量/日期",
                    measure_id,
                )

    # 验收覆盖：引用计量存在；supersedes 链合法且不新增实物量（同计量内替代）。
    measure_ids = set(revisions)
    links_by_id = {link["link_id"]: link for link in sources.acceptance_links}
    for link in sources.acceptance_links:
        if link["measure_id"] not in measure_ids:
            error(
                "link.unknown_measure",
                f"覆盖 {link['link_id']} 引用不存在的计量 {link['measure_id']}",
                link["link_id"],
            )
        if link["status"] not in {"accepted", "rejected"}:
            error("link.bad_status", f"覆盖 {link['link_id']} 状态非法：{link['status']}", link["link_id"])
        old = link["supersedes"]
        if old is not None:
            if old == link["link_id"]:
                error("link.self_supersede", f"覆盖 {link['link_id']} 不能替代自己", link["link_id"])
            elif old not in links_by_id:
                error("link.unknown_supersedes", f"覆盖 {link['link_id']} 替代目标 {old} 不存在", link["link_id"])
            elif links_by_id[old]["measure_id"] != link["measure_id"]:
                error(
                    "link.cross_measure_supersede",
                    f"覆盖 {link['link_id']} 跨计量替代 {old}，替代只在同一计量内有效",
                    link["link_id"],
                )

    # 变更：清单项存在；追溯改价必须携带新单价；纯范围变化不得携带改价口径。
    for variation in sources.variations:
        for key in variation["from_items"] + variation["to_items"]:
            if key not in item_keys:
                error(
                    "variation.unknown_item",
                    f"变更 {variation['variation_id']} 引用不存在的清单版本 {key}",
                    variation["variation_id"],
                )
        if variation["retroactive"] and variation["unit_price_fen"] is None:
            error(
                "variation.retro_without_price",
                f"追溯变更 {variation['variation_id']} 缺少新单价",
                variation["variation_id"],
            )
        if not variation["retroactive"] and variation["unit_price_fen"] is not None:
            warning(
                "variation.range_change_with_price",
                f"变更 {variation['variation_id']} 为非追溯范围变化，所带单价不回改历史，仅登记",
                variation["variation_id"],
            )

    # 支付：冲销目标存在；负数（退款）必须有 reverses 且引用明确范围。
    payments_by_id = {p["payment_id"]: p for p in sources.payments}
    acceptance_ids = {link["acceptance_id"] for link in sources.acceptance_links}
    for payment in sources.payments:
        for acc_id in payment["acceptance_ids"]:
            if acc_id not in acceptance_ids:
                error(
                    "payment.unknown_acceptance",
                    f"支付 {payment['payment_id']} 引用不存在的验收单 {acc_id}",
                    payment["payment_id"],
                )
        if payment["amount_fen"] == 0:
            error("payment.zero_amount", f"支付 {payment['payment_id']} 金额为零", payment["payment_id"])
        if payment["amount_fen"] < 0 and payment["reverses"] is None:
            error(
                "payment.refund_without_reverses",
                f"退款 {payment['payment_id']} 未指明冲销的原支付，禁止无主退款",
                payment["payment_id"],
            )
        if payment["reverses"] is not None:
            target = payments_by_id.get(payment["reverses"])
            if target is None:
                error(
                    "payment.unknown_reverses",
                    f"支付 {payment['payment_id']} 冲销目标 {payment['reverses']} 不存在",
                    payment["payment_id"],
                )
            elif target["reverses"] is not None:
                error(
                    "payment.chain_reversal",
                    f"不允许连环冲销：{payment['payment_id']} 冲销的 {target['payment_id']} 本身是冲销单",
                    payment["payment_id"],
                )
    return findings


def hard_errors(findings):
    return [f for f in findings if f["level"] == "error"]
