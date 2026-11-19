# 来源资料约定

工程清单、计量、验收覆盖、变更和现金来源资料。

所有人员、机构、地址、业务事件及数值均为合成，不来自真实企业或个人。UTF-8 编码，日期为 ISO 8601，时间附明确时区。有效区间左闭右开，空结束值为尚无结束记录，不据此推断未来。金额使用整数分，非整数数量用十进制字符串；未知不是零。

按item_key而非line_no解释版本。supersedes明确替代旧认可覆盖，不新增实物量。计量被更正后若认可合计超量，所有竞争覆盖需显示争议，不能按单据先后选赢家；未争议量独立保留。追溯价格仅针对明确清单与日期，纯范围变化不回改旧价。未给价值分解的整栋金额保留在不可分解层。退款只冲明确原支付及范围，不能自动减少工程量或删除验收。无法分解的现金覆盖保留未解释差额，禁止平均分摊。

## bill_items

清单版本行，编号可跨版本复用。

- item_key：版本行键
- contract_id：合同
- line_no：行号
- version：版本
- description：项目
- unit：单位
- unit_price_fen：单价分，空则不可分
- total_fen：总价分
- price_from：开始
- price_until：结束

## measures

同计量号修订不是新增工程量。

- measure_id：计量
- revision：修订
- item_key：版本行
- space_id：空间
- quantity：数量
- measured_on：日期

## acceptance_links

认可覆盖有明确数量。

- link_id：覆盖
- acceptance_id：验收单
- measure_id：计量
- quantity：认可数量
- status：状态
- supersedes：替代覆盖

## variations

变更不产生实物工程。

- variation_id：变更
- from_items：原清单
- to_items：新清单
- effective_date：生效
- retroactive：追溯价
- unit_price_fen：新单价分或空

## payments

现金与认可工程价值不同。

- payment_id：流水
- amount_fen：分，负为退款
- acceptance_ids：覆盖验收范围
- occurred_on：发生日
- reverses：退款原支付
