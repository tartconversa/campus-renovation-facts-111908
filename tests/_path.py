"""路径引导：隔离模式(-I)下 unittest discover 只把 tests/ 放入 sys.path，
在此显式注入仓库根，保证 factwarehouse 与 support 包可被测试模块导入。

每个测试模块的第一行业务导入为 ``from tests._path import REPO_ROOT`` 形式，
或直接 ``from . import _path`` 不适用（discovery 下无包），故本文件自执行注入。
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
