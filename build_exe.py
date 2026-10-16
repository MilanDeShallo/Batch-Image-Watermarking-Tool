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
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "WatermarkTool"
ARCHIVE = ROOT / "dist" / "_archive"
EXE_NAME = "批量图片水印工具.exe"

# psutil 也在检查列表里：它缺失不影响启动（available_memory_bytes 会回落到
# 2GB 保守预算），但会让并行度决策永远按兜底值算，而界面上完全看不出来 ——
# 属于"不装也能跑、装了才有正确行为"，必须在打包前拦住。
REQUIRED = ["customtkinter", "tkinterdnd2", "PIL", "reportlab", "pypdf",
            "pypdfium2", "psutil"]
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


def newest_source_mtime() -> float:
    """src/ 与 main.py 里最新的修改时间"""
    newest = 0.0
    for path in list((ROOT / "src").rglob("*.py")) + [ROOT / "main.py"]:
        newest = max(newest, path.stat().st_mtime)
    return newest


def make_dist_zip(app_name: str, version: str, exe: Path) -> Path | None:
    """
    生成分发包 dist/WatermarkTool-v<版本>.zip，返回路径（失败返回 None）。

    为什么放在打包脚本里、而不是"打完包再手动 zip"：
    手动那一步忘过一次 —— dist 里躺着一个比 exe 旧 2.5 小时的 zip，
    差点把它当新版发出去。凡是"改完源码必须重新做一遍"的动作，
    都应该挂在同一个入口上，不靠人记得。

    包结构（README 与用户都按这个用）：根目录单一 WatermarkTool/，
    内含 exe 与 _internal/；开发侧的 .bat 启动器不进包。
    """
    # 硬防线：exe 必须比源码新，否则打出来的就是旧程序
    exe_mtime = exe.stat().st_mtime
    src_mtime = newest_source_mtime()
    if exe_mtime < src_mtime:
        print(f"\n[警告] 产物比源码旧 {(src_mtime - exe_mtime) / 60:.1f} 分钟，"
              f"跳过打包分发包")
        return None

    zip_path = ROOT / "dist" / f"WatermarkTool-v{version}.zip"
    if zip_path.exists():
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(zip_path.stat().st_mtime)
        )
        target = ARCHIVE / f"{zip_path.stem}-{stamp}.zip"
        shutil.move(str(zip_path), str(target))
        print(f"[归档] {zip_path.name} -> {target.relative_to(ROOT)}")

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(DIST.rglob("*")):
            if path.is_dir() or path.name.endswith(".log"):
                continue
            arc = Path("WatermarkTool") / path.relative_to(DIST)
            zf.write(path, str(arc).replace("\\", "/"))
            count += 1

    # 自检：包里必须真的有 exe，路径必须是单一 WatermarkTool/ 根
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not any(n.endswith(EXE_NAME) for n in names):
            print("[警告] 分发包里没有 exe")
            return None
        if not all(n.startswith("WatermarkTool/") for n in names):
            print("[警告] 分发包内路径不是单一 WatermarkTool/ 根目录")
            return None
        stray = [n for n in names if n.lower().endswith(".bat")]
        if stray:
            print(f"[警告] 包里混进了开发侧启动器：{stray[:3]}")
    print(f"       分发包 {count} 个文件")
    return zip_path


def main() -> int:
    # 软件名与版本号只有一处定义（src/__init__.py），横幅也从那里取 ——
    # 否则改名/改版本时日志与产物会对不上，还得靠人记得改两处
    from src import APP_NAME, __version__

    print("=" * 52)
    print(f"  {APP_NAME} - 批量图片水印工具 打包")
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

    zip_path = make_dist_zip(APP_NAME, __version__, exe)

    print()
    print(f"[完成] {exe}")
    print(f"       {APP_NAME} v{__version__}")
    print(f"       大小 {exe.stat().st_size / 1024 / 1024:.1f} MB")
    if zip_path is not None:
        print(f"[完成] {zip_path}")
        print(f"       分发包 {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
    print()
    print("下一步：python tests/smoke_exe.py")
    print("        验证产物里确实包含最新改动（只看时间戳是不够的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
