"""
一键打包脚本（由 build_exe.bat 调用）。

为什么逻辑放在 .py 而不是 .bat：
cmd.exe 按**当前控制台代码页**解析批处理文件，文件编码与代码页不一致时，
中文多字节序列会被误当成命令分隔符 —— UTF-8（带/不带 BOM）与 GBK
三种写法实测都会出问题。所以 .bat 只做纯 ASCII 启动器，
所有输出与判断都在这里，并在启动时显式把 stdout 切成 UTF-8。

与旧脚本的两处关键差异：
1. **不再 `rmdir /s /q dist\\WatermarkTool`**：产物上千个文件，删除会触发
   安全删除守卫，而且删掉就没法回退到上一个可用版本。改为归档到
   dist/_archive/。
2. **依赖缺失时不再装无版本号的包**：那与 requirements.txt 的"版本已固定"
   自相矛盾。改为按 requirements.txt 安装，并走国内镜像（直连 PyPI 实测
   约 16 KB/s，装一轮依赖要一小时）。
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "WatermarkTool"
ARCHIVE = ROOT / "dist" / "_archive"
EXE_NAME = "批量图片水印工具.exe"

REQUIRED = ["customtkinter", "tkinterdnd2", "PIL", "reportlab", "pypdf", "pypdfium2"]
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def missing_deps() -> list:
    return [m for m in REQUIRED if importlib.util.find_spec(m) is None]


def install_deps() -> bool:
    print(f"[依赖] 按 requirements.txt 安装（镜像：{MIRROR}）")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-i", MIRROR,
         "-r", str(ROOT / "requirements.txt")],
        cwd=str(ROOT),
    )
    return result.returncode == 0


def check_deps() -> bool:
    missing = missing_deps()
    if not missing:
        return True

    print(f"[错误] 缺少依赖：{', '.join(missing)}")
    try:
        answer = input("       现在按 requirements.txt 安装吗？[y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("       已跳过。也可以手动执行：")
        print(f"       pip install -i {MIRROR} -r requirements.txt")
        return False

    return install_deps() and not missing_deps()


def archive_previous_build() -> None:
    """把上一次的产物挪走而不是删掉（可回退，也不触发安全删除守卫）"""
    if not DIST.exists():
        return
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE / f"WatermarkTool_{time.strftime('%Y%m%d_%H%M%S')}"
    print(f"[归档] {DIST.relative_to(ROOT)} -> {target.relative_to(ROOT)}")
    shutil.move(str(DIST), str(target))


def main() -> int:
    print("=" * 52)
    print("  批量图片水印工具 - 打包")
    print("=" * 52)

    if not check_deps():
        return 1

    # build/ 是 PyInstaller 的中间产物，--noconfirm 会自行覆盖，
    # 不需要手动删（删它同样可能撞上安全删除守卫）
    archive_previous_build()

    print("[打包] PyInstaller 运行中……")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "build.spec", "--noconfirm"],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print(f"\n[失败] PyInstaller 退出码 {result.returncode}")
        return 1

    exe = DIST / EXE_NAME
    if not exe.exists():
        print(f"\n[失败] 没有找到产物：{exe}")
        return 1

    from src import __version__          # noqa: E402  (需要 ROOT 在 sys.path)

    print()
    print(f"[完成] {exe}")
    print(f"       版本 v{__version__}")
    print(f"       大小 {exe.stat().st_size / 1024 / 1024:.1f} MB")
    print()
    print("下一步：python tests/smoke_exe.py")
    print("        验证产物里确实包含最新改动（只看时间戳是不够的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
