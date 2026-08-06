"""
批量图片水印工具 - 入口文件
"""

import sys
import os

# 确保项目根目录在 sys.path 中（支持直接运行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.ui.app_window import AppWindow


if __name__ == "__main__":
    app = AppWindow()
    app.mainloop()
