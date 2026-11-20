"""构造隔离的合成来源目录（测试夹具，不触碰仓库内 data/）。"""
from pathlib import Path

import _path  # noqa: F401  隔离模式路径引导
from factwarehouse.ingest import write_catalog

BILL_FIELDS = [
    "item_key", "contract_id", "line_no", "version", "description", "unit",
    "unit_price_fen", "total_fen", "price_from", "price_until",
]
MEASURE_FIELDS = ["measure_id", "revision", "item_key", "space_id", "quantity", "measured_on"]
LINK_FIELDS = ["link_id", "acceptance_id", "measure_id", "quantity", "status", "supersedes"]
VARIATION_FIELDS = [
    "variation_id", "from_items", "to_items", "effective_date", "retroactive",
    "unit_price_fen",
]
PAYMENT_FIELDS = ["payment_id", "amount_fen", "acceptance_ids", "occurred_on", "reverses"]


def bill(key, line, version, desc, unit, price, total, frm, until=""):
    return {
        "item_key": key, "contract_id": "K1", "line_no": line, "version": version,
        "description": desc, "unit": unit, "unit_price_fen": price, "total_fen": total,
        "price_from": frm, "price_until": until,
    }


def measure(mid, rev, item, space, qty, on):
    return {
        "measure_id": mid, "revision": rev, "item_key": item, "space_id": space,
        "quantity": qty, "measured_on": on,
    }


def link(lid, acc, mid, qty, status="accepted", supersedes=None):
    return {
        "link_id": lid, "acceptance_id": acc, "measure_id": mid, "quantity": qty,
        "status": status, "supersedes": supersedes,
    }


def variation(vid, frm, to, effective, retroactive, price=None):
    return {
        "variation_id": vid, "from_items": frm, "to_items": to,
        "effective_date": effective, "retroactive": retroactive,
        "unit_price_fen": price,
    }


def payment(pid, amount, accs, on, reverses=None):
    return {
        "payment_id": pid, "amount_fen": amount, "acceptance_ids": accs,
        "occurred_on": on, "reverses": reverses,
    }


def build_source_dir(base, bills, measures, links, variations, payments):
    """在 base 下生成 data/ 与自洽 catalog（含真实指纹），返回 base 路径。"""
    base = Path(base)
    write_catalog(
        base,
        [
            ("data/bill_items.csv", "csv", BILL_FIELDS, bills),
            ("data/measures.json", "json", MEASURE_FIELDS, measures),
            ("data/acceptance_links.json", "json", LINK_FIELDS, links),
            ("data/variations.json", "json", VARIATION_FIELDS, variations),
            ("data/payments.json", "json", PAYMENT_FIELDS, payments),
        ],
    )
    return base
