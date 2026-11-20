"""钻取查询：所有答案从只读事实仓现查，不缓存、不预连接宽表。

两条主链路：
- 认可金额 -> 价值行 -> 验收覆盖 -> 计量修订/空间 -> 清单版本与价格时间线；
- 一笔支付 -> 声明验收范围 -> 各状态价值依据 -> 未解释差额与失去依据部分。
"""
from .storage import open_warehouse


def overview(db_path):
    """四边界总览，用于一眼看清实物/验收/价值/现金各自口径。"""
    conn = open_warehouse(db_path)
    try:
        return {
            "release": dict(
                (r["meta_key"], r["meta_value"])
                for r in conn.execute("SELECT * FROM batch_meta")
            ),
            "value_fen": {
                r["value_status"]: r["amount"]
                for r in conn.execute(
                    "SELECT value_status, COALESCE(SUM(value_fen),0) AS amount "
                    "FROM value_f GROUP BY value_status"
                )
            },
            "lump_non_decomposable_fen": conn.execute(
                "SELECT COALESCE(SUM(total_fen),0) FROM lump_value_f"
            ).fetchone()[0],
            "cash": [dict(r) for r in conn.execute("SELECT * FROM v_payment_cover")],
            "spaces": [dict(r) for r in conn.execute("SELECT * FROM v_space_summary")],
            "disputed_measures": [
                r["measure_id"]
                for r in conn.execute("SELECT measure_id FROM measure_status_f WHERE is_disputed=1")
            ],
            "warnings": [
                dict(r) for r in conn.execute("SELECT code,message,ref FROM build_warning_f")
            ],
        }
    finally:
        conn.close()


def drill_value(db_path, value_id=None, acceptance_id=None):
    """从认可/争议金额向下钻到覆盖、计量修订、清单版本与适用价格。"""
    conn = open_warehouse(db_path)
    try:
        where, params = [], []
        if value_id:
            where.append("v.value_id=?")
            params.append(value_id)
        if acceptance_id:
            where.append("v.acceptance_id=?")
            params.append(acceptance_id)
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = conn.execute(
            f"""
            SELECT v.*, c.status AS link_status, c.supersedes, c.is_active, c.is_disputed,
                   p.latest_revision, p.superseded_revisions, p.is_correction,
                   b.total_fen AS contract_total_fen
            FROM value_f v
            LEFT JOIN acceptance_cover_f c ON c.link_id=v.link_id
            LEFT JOIN physical_measure_f p ON p.measure_id=v.measure_id
            LEFT JOIN bill_total_f b ON b.item_key=v.item_key
            {clause}
            ORDER BY v.value_id
            """,
            params,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["price_timeline"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT price_fen,valid_from,valid_until,source_ref "
                    "FROM price_timeline_f WHERE item_key=? ORDER BY valid_from",
                    (item["item_key"],),
                )
            ]
            item["lineage"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM variation_lineage_f WHERE from_item=? OR to_item=?",
                    (item["item_key"], item["item_key"]),
                )
            ]
            result.append(item)
        return result
    finally:
        conn.close()


def drill_measure(db_path, measure_id):
    """一张计量单的完整画像：修订留痕、覆盖关系、逐行价值、争议判定。"""
    conn = open_warehouse(db_path)
    try:
        return {
            "physical": dict(conn.execute(
                "SELECT * FROM physical_measure_f WHERE measure_id=?", (measure_id,)
            ).fetchone()),
            "raw_revisions": [
                dict(r)
                for r in conn.execute(
                    "SELECT revision,quantity,measured_on,included_in_cutoff "
                    "FROM raw_measure_f WHERE measure_id=? ORDER BY revision",
                    (measure_id,),
                )
            ],
            "status": dict(conn.execute(
                "SELECT * FROM measure_status_f WHERE measure_id=?", (measure_id,)
            ).fetchone()),
            "coverage": [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM acceptance_cover_f WHERE measure_id=? ORDER BY link_id",
                    (measure_id,),
                )
            ],
            "values": [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM value_f WHERE measure_id=? ORDER BY value_id", (measure_id,)
                )
            ],
        }
    finally:
        conn.close()


def drill_payment(db_path, payment_id):
    """从一笔支付追到已覆盖依据、未解释差额、失去认可依据部分与冲销链。"""
    conn = open_warehouse(db_path)
    try:
        payment = conn.execute(
            "SELECT * FROM payment_f WHERE payment_id=?", (payment_id,)
        ).fetchone()
        if payment is None:
            return None
        payment = dict(payment)
        payment["declared_acceptance_scope"] = [
            r["acceptance_id"]
            for r in conn.execute(
                "SELECT acceptance_id FROM payment_range_f WHERE payment_id=? ORDER BY acceptance_id",
                (payment_id,),
            )
        ]
        payment["basis_bridge"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM cash_basis_bridge_f WHERE payment_id=? ORDER BY value_id",
                (payment_id,),
            )
        ]
        if payment["reverses"]:
            target = conn.execute(
                "SELECT payment_id,amount_fen,occurred_on FROM payment_f WHERE payment_id=?",
                (payment["reverses"],),
            ).fetchone()
            payment["reverses_target"] = dict(target) if target else None
        reversed_by = conn.execute(
            "SELECT payment_id,amount_fen,occurred_on FROM payment_f WHERE reverses=?",
            (payment_id,),
        ).fetchall()
        payment["reversed_by"] = [dict(r) for r in reversed_by]
        return payment
    finally:
        conn.close()


def space_report(db_path):
    """空间粒度报告：整栋不可分解款不进入此表，杜绝按房间平均分摊。"""
    conn = open_warehouse(db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM v_space_summary")]
        lump = [dict(r) for r in conn.execute("SELECT * FROM lump_value_f")]
        return {
            "spaces": rows,
            "excluded_non_decomposable": lump,
            "rule": "无分解依据的整栋总价只列示在 excluded_non_decomposable，不分摊",
        }
    finally:
        conn.close()


def item_report(db_path):
    """清单版本粒度：合同口径总额 vs 已形成认可/争议/待认可价值与血缘。"""
    conn = open_warehouse(db_path)
    try:
        return {
            "items": [dict(r) for r in conn.execute("SELECT * FROM v_item_summary")],
            "lineage": [
                dict(r) for r in conn.execute("SELECT * FROM variation_lineage_f")
            ],
        }
    finally:
        conn.close()


def acceptance_report(db_path):
    """验收单粒度：一张验收单认可多张计量的一部分，逐覆盖列示。"""
    conn = open_warehouse(db_path)
    try:
        return [
            dict(r)
            for r in conn.execute(
                """
                SELECT c.acceptance_id, c.link_id, c.measure_id, c.link_quantity,
                       c.status, c.is_active, c.is_disputed,
                       c.recognized_quantity, c.disputed_quantity,
                       v.value_fen, v.value_status
                FROM acceptance_cover_f c
                LEFT JOIN value_f v ON v.link_id=c.link_id
                ORDER BY c.acceptance_id, c.link_id
                """
            )
        ]
    finally:
        conn.close()
