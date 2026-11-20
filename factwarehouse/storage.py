"""数据集成形与存储：SQLite 事实仓 + 只读视图 + 平面导出。

设计原则：
- 原始行(raw_*)与事实行(*_f)分层存放，原始行不删不改；
- 每个事实只存一次；跨域钻取走"桥接表"，不在事实表之间做多对多预连接，
  因此同一金额不会因合同项×空间×验收的多对多关系被重复求和；
- 视图全部基于最小事实行聚合，导出 CSV/JSON 与视图同口径。
"""
from decimal import Decimal
from pathlib import Path
import csv
import json
import sqlite3

from .money import to_jsonable

SCHEMA = """
CREATE TABLE batch_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL);

CREATE TABLE raw_bill_item_f(
  item_key TEXT, contract_id TEXT, line_no TEXT, version INTEGER,
  description TEXT, unit TEXT, unit_price_fen INTEGER, total_fen INTEGER,
  price_from TEXT, price_until TEXT);
CREATE TABLE raw_measure_f(
  measure_id TEXT, revision INTEGER, item_key TEXT, space_id TEXT,
  quantity TEXT, measured_on TEXT, included_in_cutoff INTEGER);
CREATE TABLE raw_acceptance_link_f(
  link_id TEXT, acceptance_id TEXT, measure_id TEXT, quantity TEXT,
  status TEXT, supersedes TEXT);
CREATE TABLE raw_variation_f(
  variation_id TEXT, from_items TEXT, to_items TEXT, effective_date TEXT,
  retroactive INTEGER, unit_price_fen INTEGER, included_in_cutoff INTEGER);
CREATE TABLE raw_payment_f(
  payment_id TEXT, amount_fen INTEGER, acceptance_ids TEXT, occurred_on TEXT,
  reverses TEXT, included_in_cutoff INTEGER);

CREATE TABLE physical_measure_f(
  measure_id TEXT PRIMARY KEY, item_key TEXT, space_id TEXT,
  latest_revision INTEGER, quantity TEXT, measured_on TEXT,
  superseded_revisions TEXT, is_correction INTEGER);
CREATE TABLE acceptance_cover_f(
  link_id TEXT PRIMARY KEY, acceptance_id TEXT, measure_id TEXT,
  link_quantity TEXT, status TEXT, supersedes TEXT, is_active INTEGER,
  is_disputed INTEGER, recognized_quantity TEXT, disputed_quantity TEXT);
CREATE TABLE measure_status_f(
  measure_id TEXT PRIMARY KEY, item_key TEXT, space_id TEXT,
  physical_quantity TEXT, active_cover_quantity TEXT, undisputed_quantity TEXT,
  disputed_claim_quantity TEXT, over_coverage_quantity TEXT,
  pending_quantity TEXT, is_disputed INTEGER);
CREATE TABLE value_f(
  value_id TEXT PRIMARY KEY, measure_id TEXT, link_id TEXT, acceptance_id TEXT,
  item_key TEXT, space_id TEXT, quantity TEXT, unit_price_fen INTEGER,
  price_source TEXT, value_fen INTEGER, value_status TEXT, measured_on TEXT);
CREATE TABLE lump_value_f(
  item_key TEXT PRIMARY KEY, contract_id TEXT, line_no TEXT, version INTEGER,
  total_fen INTEGER, reason TEXT);
CREATE TABLE bill_total_f(
  item_key TEXT PRIMARY KEY, contract_id TEXT, version INTEGER,
  unit_price_fen INTEGER, total_fen INTEGER, price_from TEXT, price_until TEXT);
CREATE TABLE variation_lineage_f(
  lineage_id INTEGER PRIMARY KEY AUTOINCREMENT, variation_id TEXT,
  from_item TEXT, to_item TEXT, effective_date TEXT, retroactive INTEGER,
  new_unit_price_fen INTEGER, scope_only INTEGER);
CREATE TABLE payment_f(
  payment_id TEXT PRIMARY KEY, amount_fen INTEGER, occurred_on TEXT,
  reverses TEXT, is_refund INTEGER, recognized_basis_fen INTEGER,
  disputed_basis_fen INTEGER, inactive_basis_fen INTEGER,
  claimed_basis_fen INTEGER, unexplained_fen INTEGER, basis_impaired_fen INTEGER);
CREATE TABLE payment_range_f(payment_id TEXT, acceptance_id TEXT);
CREATE TABLE cash_basis_bridge_f(
  bridge_id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id TEXT,
  acceptance_id TEXT, value_id TEXT, value_status TEXT, basis_value_fen INTEGER,
  allocated_basis_fen INTEGER, allocation_state TEXT);
CREATE TABLE price_timeline_f(
  timeline_id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT, price_fen INTEGER,
  valid_from TEXT, valid_until TEXT, source_ref TEXT);
CREATE TABLE build_warning_f(warning_id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT, message TEXT, ref TEXT);

-- 计量粒度：实物/认可/争议/待认可一屏对照
CREATE VIEW v_measure_status AS SELECT * FROM measure_status_f;

-- 空间粒度：仅可计价实物，整栋总价绝不参与分摊
CREATE VIEW v_space_summary AS
SELECT m.space_id,
       m.item_key,
       SUM(CAST(m.physical_quantity AS REAL)) AS physical_quantity,
       SUM(CAST(m.undisputed_quantity AS REAL)) AS recognized_quantity,
       SUM(CAST(m.disputed_claim_quantity AS REAL)) AS disputed_quantity,
       SUM(CAST(m.pending_quantity AS REAL)) AS pending_quantity,
       COALESCE(SUM(CASE WHEN v.value_status='recognized' THEN v.value_fen END),0) AS recognized_value_fen,
       COALESCE(SUM(CASE WHEN v.value_status='disputed' THEN v.value_fen END),0) AS disputed_value_fen,
       COALESCE(SUM(CASE WHEN v.value_status='pending' THEN v.value_fen END),0) AS pending_value_fen
FROM measure_status_f m
LEFT JOIN value_f v ON v.measure_id=m.measure_id
GROUP BY m.space_id, m.item_key;

-- 清单版本粒度：合同口径 vs 已形成认可价值
CREATE VIEW v_item_summary AS
SELECT b.item_key, b.version, b.unit_price_fen, b.total_fen AS contract_total_fen,
  COALESCE((SELECT SUM(value_fen) FROM value_f WHERE item_key=b.item_key AND value_status='recognized'),0) AS recognized_value_fen,
  COALESCE((SELECT SUM(value_fen) FROM value_f WHERE item_key=b.item_key AND value_status='disputed'),0) AS disputed_value_fen,
  COALESCE((SELECT SUM(value_fen) FROM value_f WHERE item_key=b.item_key AND value_status='pending'),0) AS pending_value_fen
FROM bill_total_f b;

-- 现金口径：每笔支付只出现一次，未解释差额显式列示
CREATE VIEW v_payment_cover AS
SELECT payment_id, amount_fen, occurred_on, reverses, is_refund,
       recognized_basis_fen, disputed_basis_fen, inactive_basis_fen,
       claimed_basis_fen, unexplained_fen, basis_impaired_fen
FROM payment_f;
"""

FACT_TABLES = [
    "physical_measure_f",
    "acceptance_cover_f",
    "measure_status_f",
    "value_f",
    "lump_value_f",
    "bill_total_f",
    "variation_lineage_f",
    "payment_f",
    "payment_range_f",
    "cash_basis_bridge_f",
    "price_timeline_f",
    "build_warning_f",
]

RAW_TABLES = [
    "raw_bill_item_f",
    "raw_measure_f",
    "raw_acceptance_link_f",
    "raw_variation_f",
    "raw_payment_f",
]


def _adapt(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=to_jsonable)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


def build_database(db_path, sources, facts, timeline, meta):
    """在全新的 SQLite 文件中成形一期事实仓。已存在则报错，防止覆盖移交期。"""
    db_path = Path(db_path)
    if db_path.exists():
        raise FileExistsError(f"事实仓已存在，移交期不可改写：{db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO batch_meta(meta_key,meta_value) VALUES(?,?)",
            sorted(meta.items()),
        )

        for bill in sources.bill_items:
            conn.execute(
                "INSERT INTO raw_bill_item_f VALUES(?,?,?,?,?,?,?,?,?,?)",
                [_adapt(bill[k]) for k in (
                    "item_key", "contract_id", "line_no", "version", "description",
                    "unit", "unit_price_fen", "total_fen", "price_from", "price_until")],
            )
        for measure in sources.measures:
            conn.execute(
                "INSERT INTO raw_measure_f VALUES(?,?,?,?,?,?,?)",
                [_adapt(measure[k]) for k in (
                    "measure_id", "revision", "item_key", "space_id", "quantity",
                    "measured_on", "included_in_cutoff")],
            )
        for link in sources.acceptance_links:
            conn.execute(
                "INSERT INTO raw_acceptance_link_f VALUES(?,?,?,?,?,?)",
                [_adapt(link[k]) for k in (
                    "link_id", "acceptance_id", "measure_id", "quantity", "status", "supersedes")],
            )
        for variation in sources.variations:
            conn.execute(
                "INSERT INTO raw_variation_f VALUES(?,?,?,?,?,?,?)",
                [_adapt(variation[k]) for k in (
                    "variation_id", "from_items", "to_items", "effective_date",
                    "retroactive", "unit_price_fen", "included_in_cutoff")],
            )
        for payment in sources.payments:
            conn.execute(
                "INSERT INTO raw_payment_f VALUES(?,?,?,?,?,?)",
                [_adapt(payment[k]) for k in (
                    "payment_id", "amount_fen", "acceptance_ids", "occurred_on",
                    "reverses", "included_in_cutoff")],
            )

        _insert_many(conn, "physical_measure_f", facts["physical_measure_f"], [
            "measure_id", "item_key", "space_id", "latest_revision", "quantity",
            "measured_on", "superseded_revisions", "is_correction"])
        _insert_many(conn, "acceptance_cover_f", facts["acceptance_cover_f"], [
            "link_id", "acceptance_id", "measure_id", "link_quantity", "status",
            "supersedes", "is_active", "is_disputed", "recognized_quantity", "disputed_quantity"])
        _insert_many(conn, "measure_status_f", facts["measure_status_f"], [
            "measure_id", "item_key", "space_id", "physical_quantity", "active_cover_quantity",
            "undisputed_quantity", "disputed_claim_quantity", "over_coverage_quantity",
            "pending_quantity", "is_disputed"])
        _insert_many(conn, "value_f", facts["value_f"], [
            "value_id", "measure_id", "link_id", "acceptance_id", "item_key", "space_id",
            "quantity", "unit_price_fen", "price_source", "value_fen", "value_status", "measured_on"])
        _insert_many(conn, "lump_value_f", facts["lump_value_f"], [
            "item_key", "contract_id", "line_no", "version", "total_fen", "reason"])
        _insert_many(conn, "bill_total_f", facts["bill_total_f"], [
            "item_key", "contract_id", "version", "unit_price_fen", "total_fen",
            "price_from", "price_until"])
        _insert_many(conn, "variation_lineage_f", facts["variation_lineage_f"], [
            "variation_id", "from_item", "to_item", "effective_date", "retroactive",
            "new_unit_price_fen", "scope_only"])
        _insert_many(conn, "payment_f", facts["payment_f"], [
            "payment_id", "amount_fen", "occurred_on", "reverses", "is_refund",
            "recognized_basis_fen", "disputed_basis_fen", "inactive_basis_fen",
            "claimed_basis_fen", "unexplained_fen", "basis_impaired_fen"])
        _insert_many(conn, "payment_range_f", facts["payment_range_f"],
                     ["payment_id", "acceptance_id"])
        _insert_many(conn, "cash_basis_bridge_f", facts["cash_basis_bridge_f"], [
            "payment_id", "acceptance_id", "value_id", "value_status", "basis_value_fen",
            "allocated_basis_fen", "allocation_state"])
        for item_key, segments in sorted(timeline.items()):
            for segment in segments:
                conn.execute(
                    "INSERT INTO price_timeline_f(item_key,price_fen,valid_from,valid_until,source_ref)"
                    " VALUES(?,?,?,?,?)",
                    [item_key, segment.price_fen, segment.valid_from.isoformat(),
                     segment.valid_until.isoformat() if segment.valid_until else None, segment.source],
                )
        _insert_many(conn, "build_warning_f", facts["warnings"],
                     ["code", "message", "ref"])
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_many(conn, table, rows, columns):
    if not rows:
        return
    placeholders = ",".join("?" for _ in columns)
    conn.executemany(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        [[_adapt(row.get(col)) for col in columns] for row in rows],
    )


def open_warehouse(db_path):
    """只读连接，查询层统一走这里，杜绝查询误写移交期数据。"""
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def export_flat(db_path, out_dir):
    """把事实表与视图导出为同口径 CSV，外加 warnings.json。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = open_warehouse(db_path)
    try:
        objects = FACT_TABLES + RAW_TABLES + [
            "v_measure_status", "v_space_summary", "v_item_summary", "v_payment_cover"]
        for name in objects:
            rows = conn.execute(f"SELECT * FROM {name}").fetchall()
            with (out_dir / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(rows[0].keys() if rows else [
                    r[1] for r in conn.execute(f"PRAGMA table_info({name})")])
                writer.writerows([tuple(row) for row in rows])
        warnings = [dict(r) for r in conn.execute(
            "SELECT code,message,ref FROM build_warning_f ORDER BY warning_id")]
        (out_dir / "build_warnings.json").write_text(
            json.dumps(warnings, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        conn.close()
    return out_dir
