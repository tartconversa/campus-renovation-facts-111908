# 改造工程变更与分期验收的量价事实仓

闲置校舍改造为养老服务点场景下，把合同清单、设计变更、现场计量、验收结论和支付凭据
成形为各有粒度、可钻取、可分期移交的 Python 量价事实仓。仅使用 Python 3.14 标准库，
SQLite 为内置存储，离线可运行、无第三方依赖。

核心立场：**累计付款不是工程完成**。实物、验收、价值、现金是四条各自守恒的边界，
任何报表只能在各自边界内聚合，再通过显式桥接关系互相解释，不能互相替代。

## 目录

- `data/` 合成来源资料与 `catalog.json` 指纹登记（原始资料，不被程序改写）
- `support/check_inputs.py` 来源完整性自检（只验资料，不产出业务答案）
- `factwarehouse/` 事实仓程序
  - `ingest.py` 原始资料接收：指纹核验、类型解析、截止日分期留痕
  - `validate.py` 相互核对：引用完整性与业务规则，error 阻断、warning 留痕
  - `pricing.py` 清单价格时间线：追溯改价与纯范围变化的严格区分
  - `facts.py` 事实构建：实物/验收/价值/不可分解/现金五类事实
  - `storage.py` SQLite 成形、防扇形展开视图、CSV/JSON 导出
  - `conservation.py` 四边界守恒独立重算（23 项检查）
  - `release.py` 不可变移交期、清单固化、期间差异
  - `query.py` 钻取查询；`cli.py` 命令行入口
- `fw.py` 隔离模式启动器（配合 `-I`，防来源目录同名模块劫持标准库）
- `tests/` 37 个自动化测试（资料自检、四边界、负向校验、钻取与 CLI）
- `docs/source-contract.md` 来源约定；`docs/factwarehouse.md` 语义与运行说明

## 快速开始

全部命令以 `-I` 隔离模式运行（屏蔽 cwd 与 PYTHONPATH，防止来源资料中植入的
`json.py` 等同名模块被解释器加载）：

```bash
# 1. 来源完整性自检（指纹、条数、字段、引用）
python -I support/check_inputs.py

# 2. 业务相互核对（error/warning）
python -I fw.py validate

# 3. 成形一期不可变事实仓，内含四边界守恒验证
python -I fw.py build --release R001

# 4. 独立重算守恒（不依赖构建期结果）
python -I fw.py verify --release R001

# 5. 钻取查询
python -I fw.py overview  --release R001      # 四边界总览
python -I fw.py spaces    --release R001      # 空间粒度（整栋款不分摊）
python -I fw.py items     --release R001      # 清单版本与变更血缘
python -I fw.py acceptance --release R001     # 验收单×计量多对多覆盖
python -I fw.py measure   --release R001 --measure-id M1   # 单计量完整画像
python -I fw.py value     --release R001 --acceptance-id AC1  # 金额→覆盖→计量→版本
python -I fw.py payment   --release R001 --payment-id P1    # 支付→依据/未解释/冲销链

# 6. 平面导出（CSV + warnings.json，位于 state/releases/R001/exports/）
python -I fw.py export --release R001

# 7. 分期：按截止日成形（带日期事实按期过滤，被排除行原始留痕）
python -I fw.py build --release R002 --cutoff 2026-10-22
python -I fw.py diff --from R001 --to R002   # 期间差异，旧期不被改写

# 8. 全量自动化测试
python -I -m unittest discover -s tests -v
```

## 四条守恒边界（详见 docs/factwarehouse.md）

| 边界 | 守恒口径 | 关键规则 |
| --- | --- | --- |
| 实物 physical | 实物量只计每计量号最新修订 | 同号修订是替代不是新增，旧修订原始留痕 |
| 验收 acceptance | 有效认可 = 无争议认可 + 争议主张 | 超量覆盖全部争议、不选赢家；替代/驳回不增量 |
| 价值 value | 逐行数量 × 事件适用单价 | 追溯价不回算更早事件；整栋总价留不可分解层 |
| 现金 cash | 依据分量 + 未解释差额 = 付款绝对额 | 支付不充作完成量；退款只冲明确范围，不自动生成 |

`verify` 全部检查通过时 `all_passed=true`；任一失败，该期不得描述为已核验。

## 来源指纹说明

`data/catalog.json` 的 SHA-256 指纹按仓库实际字节（LF 行尾）登记；`.gitattributes`
锁定文本文件 LF，避免行尾漂移造成指纹失配。任何来源内容改动都应重新计算指纹，
否则接收阶段直接拒绝成形。

## 容器

```bash
docker build -t campus-renovation-facts .
docker run --rm --network none campus-renovation-facts
docker run --rm --network none campus-renovation-facts \
  python -I -m unittest discover -s tests -v
```
