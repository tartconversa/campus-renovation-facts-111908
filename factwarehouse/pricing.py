"""适用价格解析：清单价格时间线 + 追溯改价。

语义裁决（与 docs/source-contract.md 一致，并在此固化）：

- 清单版本行自带初始价格段，区间左闭右开（price_from 含、price_until 不含），
  空 price_until 表示尚无结束记录，不据此推断未来价格。
- retroactive=true 且携带单价的变更：把新单价回溯登记到"明确清单项 + 生效日"，
  即对该清单项 measured_on >= effective_date 的计量/认可事件适用新价；
  更早的事件仍按旧价，绝不以当前单价重算全部历史。旧价格段保留可查。
- retroactive=false 的纯范围变化（如整栋拆项）不写入任何价格段，
  只登记价值来源血缘；新清单项自身的初始价格仍来自清单行。
- 无单价清单（整栋总价）没有价格时间线，计价层拒绝逐量计价，
  其 total_fen 保留在不可分解层，禁止平均分摊。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceSegment:
    item_key: str
    price_fen: int
    valid_from: object
    valid_until: object
    source: str  # bill:<item_key> 或 variation:<variation_id>


def build_timelines(sources):
    """返回 {item_key: [PriceSegment, ...]}，按生效日升序、相邻段首尾相接。"""
    timeline = {}
    for bill in sources.bill_items:
        if bill["unit_price_fen"] is None:
            continue  # 不可分清单，不建立价格时间线
        timeline.setdefault(bill["item_key"], []).append(
            PriceSegment(
                item_key=bill["item_key"],
                price_fen=bill["unit_price_fen"],
                valid_from=bill["price_from"],
                valid_until=bill["price_until"],
                source=f"bill:{bill['item_key']}",
            )
        )
    for variation in sources.variations:
        if not variation.get("included_in_cutoff", True):
            continue
        if not variation["retroactive"] or variation["unit_price_fen"] is None:
            continue  # 纯范围变化不回改旧价
        for item_key in variation["to_items"]:
            segments = timeline.get(item_key)
            if not segments:
                # 追溯改价目标必须是有单价的清单版本，否则无法适用。
                continue
            effective = variation["effective_date"]
            closed = []
            for segment in segments:
                if segment.valid_from < effective and (
                    segment.valid_until is None or effective < segment.valid_until
                ):
                    closed.append(
                        PriceSegment(
                            segment.item_key,
                            segment.price_fen,
                            segment.valid_from,
                            effective,
                            segment.source,
                        )
                    )
                else:
                    closed.append(segment)
            closed.append(
                PriceSegment(
                    item_key,
                    variation["unit_price_fen"],
                    effective,
                    None,
                    source=f"variation:{variation['variation_id']}",
                )
            )
            timeline[item_key] = sorted(closed, key=lambda s: s.valid_from)
    return timeline


def price_for(timeline, item_key, on_date):
    """按事件日期解析适用单价。返回 (price_fen, source)；无适用价返回 (None, 原因)。"""
    segments = timeline.get(item_key)
    if not segments:
        return None, "无单价清单项：价值保留在不可分解层"
    for segment in segments:
        if segment.valid_from <= on_date and (
            segment.valid_until is None or on_date < segment.valid_until
        ):
            return segment.price_fen, segment.source
    return None, "事件日期落在全部价格段之外，尚无适用价格记录"
