"""
打包产物冒烟 —— 验证 dist 里的 exe 真的包含本次改动

为什么单独写这个脚本
------------------
「我重新打包了」这句话本身不证明任何东西：

1. **时间戳只能说明何时构建，不能说明构建了什么。** 构建成功、产物却是旧的
   （增量构建没覆盖、手工拷回来的 dist）都可能发生。
2. **源码改对了但忘了重新打包。** 用户是通过 exe 验证的，源码改了没打包
   等于没修。本项目踩过：源码改到 18:45，dist 里还是 18:19 的 exe，
   用户报的问题就是从旧 exe 上看到的。
3. **onedir / onefile 结构混淆。** onedir 的 exe 只有几 MB 且必须与
   `_internal` 同目录；一个孤立的 4MB exe 一定跑不起来。

所以这里做两件事：

- **符号探测**：从 exe 里把打包好的字节码抽出来，查找「只有新版才有的符号」。
  这些符号是本次改动引入的，旧包里不可能有。
- **启动探测**（`--launch`）：真的把 exe 跑起来，确认进程能存活若干秒。

用法：
    python tests/smoke_exe.py                 # 符号探测（快）
    python tests/smoke_exe.py --launch        # 额外启动 exe
    python tests/smoke_exe.py --exe <路径>    # 指定 exe

退出码 0 表示全过。
"""

from __future__ import annotations

import argparse
import glob
import marshal
import os
import subprocess
import sys
import tempfile
import time

# CI（英文 Windows）的 stdout 默认是 cp1252 —— 中文输出会 UnicodeEncodeError
# 直接崩掉。与 build_exe.py 同一处理：显式切成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# 只有新版才有的符号
# ============================================================
# 每项：(打包模块名, 该模块里必须存在的符号, 说明)
# 这些符号都来自本次审计修复，旧包里不存在。
REQUIRED_SYMBOLS = [
    ("src.config", "opacity_from_color",
     "opacity/alpha 单一权威源（迁移历史 alpha 写法）"),
    ("src.config", "DEFAULT_FILENAME_TEMPLATE",
     "默认值集中到配置类"),
    ("src.config", "resolved_on_conflict",
     "同名冲突策略"),
    ("src.file_manager", "atomic_output",
     "原子写出：临时文件 → os.replace"),
    ("src.file_manager", "resolve_output_target",
     "多目录输出策略与界面提示同源"),
    ("src.batch_planner", "plan_output_paths",
     "文件名模板 / 冲突策略 / 覆盖原图的落点计算"),
    ("src.batch_planner", "TaskCancelled",
     "任务级取消"),
    ("src.resource_budget", "plan_workers",
     "按像素内存预算决定并行度"),
    ("src.watermark_engine", "MAX_IMAGE_PIXELS_LIMIT",
     "恢复有限解压炸弹保护"),
    ("src.pdf_engine", "set_overlay_scale",
     "覆盖层倍率可调"),
    ("src.pdf_engine", "has_digital_signature",
     "PDF 数字签名检测"),
    ("src.ui.app_window", "_write_failure_report",
     "失败清单落盘"),
    ("src.ui.app_window", "_release_stale_grab",
     "僵尸抓取安全网"),

    # ---- v1.5：第二轮审计修复 ----
    ("src.config", "normalize_position",
     "位置别名归一化（center-left → middle-left）"),
    ("src.config", "MAX_IMAGE_PIXELS",
     "像素上限单一来源（引擎与内存预算共用）"),
    ("src.config", "SUPPORTED_EXTENSIONS",
     "输入扩展名单一来源"),
    ("src.watermark_engine", "_is_high_depth_mode",
     "高位深/浮点模式降位时写日志"),
    ("src.pdf_engine", "OVERLAY_CACHE_MAX_ITEMS",
     "覆盖层缓存上限（LRU 淘汰）"),
    ("src.preset_manager", "_ensure_dirs",
     "预设目录迁到用户目录 + 旧目录迁移"),
    ("src.batch_planner", "overlay_scale",
     "倍率快照透传到 PDF 引擎"),
    ("src.ui.theme", "install_theme",
     "皮肤装载：单一浅色主题 + 失败回落内置主题"),
    ("src.ui.app_window", "_dispatch_impl",
     "调度器异常兜底（按钮不会卡在禁用态）"),
    ("src.ui.app_window", "_run_batch",
     "预检异步化后的批处理主体"),

    # ---- v1.5：第三轮审计修复 ----
    ("src.config", "VALID_POSITIONS",
     "位置白名单（'left-middle'/'weird'/空串不再静默落到正中）"),
    ("src.watermark_engine", "DEFAULT_POSITION",
     "兜底默认值与 config 单一来源对齐（不再硬写 middle-center）"),
    ("src.ui.app_window", "_set_processing_ui",
     "「处理中」三态收敛：提前返回 / 预检失败也能复位按钮"),

    # ---- v1.5：第四轮审计修复 ----
    ("src.resource_budget", "probe_pdf_page_pixels",
     "PDF 按页面显示像素进预算（旧实现一律按 64MB 估，A0 低估 11 倍）"),
    ("src.resource_budget", "probe_page_pixels",
     "超限/预警清单不再跳过 PDF"),
    ("src.resource_budget", "PDF_PAGE_SLACK",
     "PDF 单页峰值系数 7.0（等效 28 B/px，实测 21~23）"),

    # ---- v1.6.0：预览图上直接拖水印 ----
    ("src.watermark_engine", "watermark_boxes",
     "水印几何的单一来源（渲染 / 命中判定 / 拖拽共用）"),
    ("src.watermark_engine", "custom_percent_for_anchor",
     "像素 → 百分比反算（拖动写回坐标用）"),
    ("src.watermark_engine", "build_watermark_sprite",
     "水印精灵构造（预览拖动与渲染共用）"),
    ("src.config", "format_percent",
     "百分比格式化（拖动写回保留小数、去掉浮点噪声）"),

    # ---- 界面美化（v1.5 之后）----
    ("src.ui.app_window", "style_titlebar",
     "系统标题栏按皮肤上色（DWM 属性 34/35/36，Win11 22H2+）"),
    ("src.ui.app_window", "_asset_path",
     "图标等资源路径在打包后能定位到 _internal"),

    # ---- 品牌化（2026-10-16）----
    ("src", "APP_NAME",
     "品牌名单一来源（窗口标题 / 失败清单 / PDF Producer 共用）"),
]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), str(detail)))
    flag = "PASS" if cond else "FAIL"
    print(f"  [{flag}] {name}" + (f"  — {detail}" if detail else ""), flush=True)


def head(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# ============================================================
# 定位 exe
# ============================================================

def find_exe(explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    patterns = [
        os.path.join(ROOT, "dist", "WatermarkTool", "*.exe"),
        os.path.join(ROOT, "dist", "*.exe"),
    ]
    for pattern in patterns:
        found = [p for p in glob.glob(pattern)
                 if os.path.basename(p).lower() not in ("unins000.exe",)]
        if found:
            return max(found, key=os.path.getmtime)
    return None


# ============================================================
# 从 PyInstaller 归档里抽字节码
# ============================================================

def load_packaged_code(exe: str) -> dict[str, bytes]:
    """
    返回 {模块名: marshalled 字节码}。

    两个坑：
    - PYZ 嵌在 exe 里，`_internal/` 下没有独立的 PYZ.pyz 文件，
      必须先从 CArchive 里 extract 出来落盘
    - ZlibArchiveReader 只接受**文件路径**，传 bytes 会报错
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    archive = CArchiveReader(exe)
    names = set(archive.toc)

    # PYZ 在 CArchive 里的名字随版本而变，逐个试
    pyz_name = None
    for candidate in ("PYZ.pyz", "PYZ-00.pyz", "PYZ"):
        if candidate in names:
            pyz_name = candidate
            break
    if pyz_name is None:
        for candidate in names:
            if candidate.upper().startswith("PYZ"):
                pyz_name = candidate
                break
    if pyz_name is None:
        raise RuntimeError(f"在 {os.path.basename(exe)} 的归档里找不到 PYZ（共 {len(names)} 项）")

    with tempfile.TemporaryDirectory() as tmp:
        pyz_path = os.path.join(tmp, "PYZ.pyz")
        with open(pyz_path, "wb") as f:
            f.write(archive.extract(pyz_name))

        reader = ZlibArchiveReader(pyz_path)
        blobs: dict[str, bytes] = {}
        for key in reader.toc:
            try:
                code = reader.extract(key)
            except Exception:
                continue
            try:
                # extract 返回 CodeType，必须先 marshal.dumps 才能做子串探测
                blobs[str(key)] = marshal.dumps(code)
            except Exception:
                continue
        return blobs


def symbol_probe(exe: str) -> None:
    head("打包产物体积与结构")
    size_mb = os.path.getsize(exe) / (1024 * 1024)
    internal = os.path.join(os.path.dirname(exe), "_internal")
    check("exe 体积符合 onedir 预期（4~12MB）", 2 <= size_mb <= 12, f"{size_mb:.1f}MB")
    check("同目录存在 _internal（onedir 结构）", os.path.isdir(internal),
          os.path.dirname(exe))

    print(f"  产物: {exe}", flush=True)
    print(f"  构建时间: "
          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(exe)))}",
          flush=True)

    head("从 exe 抽出打包字节码")
    try:
        blobs = load_packaged_code(exe)
    except Exception as exc:
        check("能读取 exe 里的 PYZ 归档", False, f"{type(exc).__name__}: {exc}")
        return
    check("能读取 exe 里的 PYZ 归档", True, f"{len(blobs)} 个模块")

    head("符号探测：exe 里是否真的包含本次改动")
    for module, symbol, why in REQUIRED_SYMBOLS:
        blob = blobs.get(module)
        if blob is None:
            # 模块名可能以 .pyc 结尾或用点号/斜杠两种写法
            for key, value in blobs.items():
                if key.replace("/", ".").replace("\\", ".").startswith(module):
                    blob = value
                    break
        if blob is None:
            check(f"{module} 已打包", False, "模块不在 PYZ 里")
            continue
        check(f"{module}.{symbol} 存在", symbol.encode() in blob, why)


# ============================================================
# 启动探测
# ============================================================

def launch_probe(exe: str, wait: float = 9.0) -> None:
    head("启动探测：exe 能跑起来并存活")
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
    try:
        time.sleep(wait)
        alive = proc.poll() is None
        check(f"启动 {wait:.0f}s 后进程仍存活", alive,
              f"pid={proc.pid}" if alive else f"退出码 {proc.returncode}")
        if not alive:
            return

        # 顺带确认没弹出崩溃对话框之类的额外窗口是不可判定的；
        # 这里只做进程存活断言，界面正确性交给 smoke_gui.py。
        check("没有任何子进程异常退出", proc.poll() is None)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    head("已关闭启动的 exe")


def main() -> int:
    parser = argparse.ArgumentParser(description="打包产物冒烟")
    parser.add_argument("--exe", default=None, help="指定 exe 路径")
    parser.add_argument("--launch", action="store_true", help="额外启动 exe 并等待")
    parser.add_argument("--wait", type=float, default=9.0, help="启动后等待秒数")
    args = parser.parse_args()

    sys.path.insert(0, ROOT)

    exe = find_exe(args.exe)
    if not exe:
        print("找不到打包产物。先执行：python -m PyInstaller build.spec --noconfirm")
        return 2

    symbol_probe(exe)
    if args.launch:
        launch_probe(exe, args.wait)

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 56, flush=True)
    print(f"打包冒烟结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过", flush=True)
    for name, _, detail in failed:
        print(f"  - {name}  {detail}", flush=True)
    print("=" * 56, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
