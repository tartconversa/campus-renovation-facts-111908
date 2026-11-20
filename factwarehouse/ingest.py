"""原始资料接收：来源完整性核验与结构化装载。

职责边界（与 support/check_inputs.py 一致，且不依赖其实现）：
- 校验合成声明、路径越界、SHA-256 指纹、条数与字段集合；
- 解析为带类型的记录（日期 date、数量 Decimal、金额 int 保留）；
- 按截止日 cutoff 对"带日期的事实"做分期过滤，原始行全部留痕，
  被截止日排除的行标 included_in_cutoff=false，不静默丢弃。

本模块只做接收与解析，不做任何业务判断（超量、计价、覆盖等在后续层）。
"""
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import csv
import hashlib
import json

from .money import parse_quantity


def _read_rows(path, kind):
    if kind == "json":
        return json.loads(path.read_text(encoding="utf-8"))
    if kind == "jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if kind == "csv":
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    raise ValueError(f"来源格式无效：{kind}")


def _parse_iso_date(text):
    if text in (None, ""):
        return None
    return date.fromisoformat(str(text)[:10])


@dataclass(frozen=True)
class Sources:
    """经核验、解析后的来源记录集合。"""

    root: Path
    catalog: dict
    bill_items: list
    measures: list
    acceptance_links: list
    variations: list
    payments: list
    cutoff: object = None  # datetime.date 或 None（None 表示全量）

    def all_bundles(self):
        return {
            "bill_items": self.bill_items,
            "measures": self.measures,
            "acceptance_links": self.acceptance_links,
            "variations": self.variations,
            "payments": self.payments,
        }


def _verify_catalog(root, catalog):
    """指纹、路径、条数、字段、引用完整性核验，失败抛 AssertionError。"""
    assert catalog.get("synthetic") is True, "缺少合成资料声明"
    loaded = {}
    for entry in catalog["files"]:
        path = (root / entry["path"]).resolve()
        assert path.is_relative_to(root), f"来源路径越界：{entry['path']}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], (
            f"来源指纹不符：{entry['path']}"
        )
        rows = _read_rows(path, entry["format"])
        assert isinstance(rows, list) and len(rows) == entry["rows"], (
            f"来源条数不符：{entry['path']}"
        )
        for row in rows:
            assert isinstance(row, dict) and set(row) == set(entry["fields"]), (
                f"字段不符：{entry['path']}"
            )
        loaded[entry["path"]] = rows
    for entry in catalog["files"]:
        for ref in entry["references"]:
            target_keys = {str(row[ref["target_field"]]) for row in loaded[ref["target"]]}
            for row in loaded[entry["path"]]:
                raw = row[ref["field"]]
                if raw is None or raw == "":
                    continue
                values = raw if isinstance(raw, list) else [raw]
                assert all(str(v) in target_keys for v in values), (
                    f"引用不存在：{entry['path']}.{ref['field']}={raw!r}"
                )
    return loaded


def _parse_bill_rows(rows):
    parsed = []
    for row in rows:
        parsed.append(
            {
                "item_key": row["item_key"],
                "contract_id": row["contract_id"],
                "line_no": row["line_no"],
                "version": int(row["version"]),
                "description": row["description"],
                "unit": row["unit"] or None,
                "unit_price_fen": int(row["unit_price_fen"]) if row["unit_price_fen"] else None,
                "total_fen": int(row["total_fen"]),
                "price_from": _parse_iso_date(row["price_from"]),
                "price_until": _parse_iso_date(row["price_until"]),
            }
        )
    return parsed


def _parse_measure_rows(rows, cutoff):
    parsed = []
    for row in rows:
        measured_on = _parse_iso_date(row["measured_on"])
        parsed.append(
            {
                "measure_id": row["measure_id"],
                "revision": int(row["revision"]),
                "item_key": row["item_key"],
                "space_id": row["space_id"],
                "quantity": parse_quantity(row["quantity"]),
                "measured_on": measured_on,
                "included_in_cutoff": cutoff is None or measured_on <= cutoff,
            }
        )
    return parsed


def _parse_link_rows(rows):
    """验收覆盖无日期字段，分期过滤不适用，全部装载并保留原始状态。"""
    parsed = []
    for row in rows:
        parsed.append(
            {
                "link_id": row["link_id"],
                "acceptance_id": row["acceptance_id"],
                "measure_id": row["measure_id"],
                "quantity": parse_quantity(row["quantity"]),
                "status": row["status"],
                "supersedes": row["supersedes"],
            }
        )
    return parsed


def _parse_variation_rows(rows, cutoff):
    parsed = []
    for row in rows:
        effective = _parse_iso_date(row["effective_date"])
        parsed.append(
            {
                "variation_id": row["variation_id"],
                "from_items": list(row["from_items"]),
                "to_items": list(row["to_items"]),
                "effective_date": effective,
                "retroactive": bool(row["retroactive"]),
                "unit_price_fen": int(row["unit_price_fen"]) if row["unit_price_fen"] is not None else None,
                "included_in_cutoff": cutoff is None or effective <= cutoff,
            }
        )
    return parsed


def _parse_payment_rows(rows, cutoff):
    parsed = []
    for row in rows:
        occurred = _parse_iso_date(row["occurred_on"])
        parsed.append(
            {
                "payment_id": row["payment_id"],
                "amount_fen": int(row["amount_fen"]),
                "acceptance_ids": list(row["acceptance_ids"]),
                "occurred_on": occurred,
                "reverses": row["reverses"],
                "included_in_cutoff": cutoff is None or occurred <= cutoff,
            }
        )
    return parsed


def load_sources(root, cutoff=None, verify=True):
    """接收来源资料。cutoff 为 ISO 日期字符串或 date，用于分期。"""
    root = Path(root).resolve()
    catalog = json.loads((root / "data" / "catalog.json").read_text(encoding="utf-8"))
    loaded = _verify_catalog(root, catalog) if verify else {
        entry["path"]: _read_rows(root / entry["path"], entry["format"])
        for entry in catalog["files"]
    }
    if isinstance(cutoff, str):
        cutoff = _parse_iso_date(cutoff)
    return Sources(
        root=root,
        catalog=catalog,
        bill_items=_parse_bill_rows(loaded["data/bill_items.csv"]),
        measures=_parse_measure_rows(loaded["data/measures.json"], cutoff),
        acceptance_links=_parse_link_rows(loaded["data/acceptance_links.json"]),
        variations=_parse_variation_rows(loaded["data/variations.json"], cutoff),
        payments=_parse_payment_rows(loaded["data/payments.json"], cutoff),
        cutoff=cutoff,
    )


def write_catalog(path, files):
    """供测试构造来源目录：files 为 [(相对路径, 'json'|'csv'|'jsonl', 字段列表, 行)]。

    空表按登记字段写出表头/[]，避免空来源无法成形。
    """
    entries = []
    for rel, kind, fields, rows in files:
        target = Path(path) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        columns = list(fields) if fields else (list(rows[0].keys()) if rows else [])
        if kind == "json":
            target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        elif kind == "jsonl":
            target.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
            )
        elif kind == "csv":
            import io

            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
            target.write_text(buffer.getvalue(), encoding="utf-8")
        else:
            raise ValueError(kind)
        raw = target.read_bytes()
        entries.append(
            {
                "path": rel,
                "format": kind,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "rows": len(rows),
                "fields": columns,
                "references": [],
            }
        )
    catalog = {"synthetic": True, "files": entries}
    (Path(path) / "data").mkdir(parents=True, exist_ok=True)
    (Path(path) / "data" / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return catalog
