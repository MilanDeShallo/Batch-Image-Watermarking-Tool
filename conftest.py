"""
pytest 全局配置。

为什么需要它：以前 12 个测试文件各自写了一遍 `sys.path.insert`，
既重复又容易漏（漏掉的那个文件就只能在仓库根目录下才跑得起来）。
放在这里一次生效，测试文件只管测试本身。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
