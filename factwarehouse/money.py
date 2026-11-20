"""数量与金额的精确处理。

来源约定：金额为整数分；非整数数量为十进制字符串；未知不是零。
价值 = 数量(Decimal) × 单价(整数分)，结果按 ROUND_HALF_UP 落为整数分，
舍入规则固定且在运行说明中声明，不使用二进制浮点。
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("1")


def parse_quantity(text):
    """把来源中的数量字符串解析为 Decimal，空值/非法值显式报错。"""
    if text is None or str(text).strip() == "":
        raise ValueError("数量为空，未知不能当作零")
    value = Decimal(str(text).strip())
    if value <= 0:
        raise ValueError(f"数量必须为正：{text!r}")
    return value


def value_in_fen(quantity, unit_price_fen):
    """数量 × 单价（分），四舍五入到整数分。"""
    if unit_price_fen is None:
        raise ValueError("无单价清单项不可计价，应保留在不可分解层")
    raw = Decimal(quantity) * Decimal(int(unit_price_fen))
    return int(raw.quantize(CENT, rounding=ROUND_HALF_UP))


def to_jsonable(value):
    """供 json.dumps 使用的默认序列化器。"""
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
