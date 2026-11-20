#!/usr/bin/env python
"""隔离模式启动器：显式注入仓库路径后交给 factwarehouse.cli。

安全说明：-I 会屏蔽 cwd 与 PYTHONPATH，防止来源资料目录中被植入
json.py/csv.py 等同名模块劫持标准库导入；仓库路径由本脚本显式指定，
不依赖运行时的当前工作目录。用法：

    python -I fw.py --help
    python -I fw.py build --release R001
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from factwarehouse.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
