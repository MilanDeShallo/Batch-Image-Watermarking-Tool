"""
文件管理器 — 文件收集、去重、格式校验、输出路径生成、路径清洗

职责：
- 从拖拽/文件对话框收集图片文件路径
- 扩展名过滤（仅保留 SUPPORTED_EXTENSIONS 中的格式）
- 去重（基于规范化后的绝对路径）
- 输出目录和输出文件名生成
"""

from __future__ import annotations

import contextlib
import os
import re
from datetime import datetime
from typing import Optional

from src.config import (
    CONFLICT_MODES,
    CONFLICT_OVERWRITE,
    CONFLICT_RENAME,
    CONFLICT_SKIP,
    DEFAULT_FILENAME_TEMPLATE,
)

# 支持的文件扩展名（小写）— 图片 + PDF
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif", ".pdf"}

# 路径清洗正则
_PATH_CLEAN_RE = re.compile(r'^[\'\"{\s]+|[\'\"}\s]+$')


def sanitize_path(raw: str) -> str:
    """清除 Windows 拖拽传入的 {} 包裹和引号"""
    return _PATH_CLEAN_RE.sub('', raw.strip())


def is_image_file(path: str) -> bool:
    """检查文件扩展名是否为支持的格式（图片或 PDF）"""
    ext = os.path.splitext(path)[1].lower()
    return ext in SUPPORTED_EXTENSIONS


def is_pdf_file(path: str) -> bool:
    """检查是否为 PDF 文件"""
    return os.path.splitext(path)[1].lower() == ".pdf"


def collect_files(
    raw_paths: list[str],
    existing_files: Optional[set[str]] = None
) -> tuple[list[str], list[str]]:
    """
    从原始路径列表中收集有效的图片文件。

    返回 (added_files, skipped_files) 元组：
    - added_files: 新增的有效图片文件路径（已去重）
    - skipped_files: 被跳过的文件路径及原因（非图片 / 已存在 / 不存在）
    """
    added = []
    skipped = []
    existing = existing_files or set()

    for raw in raw_paths:
        clean = sanitize_path(raw)
        if not clean:
            continue

        # 规范化为绝对路径
        try:
            abs_path = os.path.abspath(clean)
        except Exception:
            skipped.append((clean, "无效路径"))
            continue

        # 检查是否存在
        if not os.path.isfile(abs_path):
            skipped.append((abs_path, "文件不存在"))
            continue

        # 检查是否为支持的格式（图片/PDF）
        if not is_image_file(abs_path):
            skipped.append((abs_path, "不支持的格式"))
            continue

        # 去重
        if abs_path in existing:
            skipped.append((abs_path, "重复文件"))
            continue

        added.append(abs_path)
        existing.add(abs_path)

    return added, skipped


def generate_output_path(
    src_path: str,
    output_dir: str,
    filename_template: str = DEFAULT_FILENAME_TEMPLATE,
    output_format: str = "auto",
    seq_counter: Optional[int] = None,
    existing_names: Optional[set[str]] = None,
    on_conflict: str = CONFLICT_RENAME,
    overwrite_original: bool = False
) -> Optional[str]:
    """
    根据源文件路径和模板生成输出文件路径。

    Args:
        src_path: 源图片文件的绝对路径
        output_dir: 输出目录（空字符串表示使用源文件所在目录）
        filename_template: 文件名模板，支持变量：
            - {original}: 原文件名（不含扩展名）
            - {date}: 处理日期 YYYYMMDD
            - {time}: 处理时间 HHMMSS
            - {seq}: 自增序号
        output_format: 输出格式 ("auto" | "png" | "jpeg")
        seq_counter: 自增序号（若模板含 {seq}）
        existing_names: 已存在的文件名集合（用于检测重名）
        on_conflict: 同名输出文件的处理策略
            - "rename"（默认）：自动加后缀，绝不覆盖已有文件
            - "overwrite"：磁盘上同名直接覆盖
            - "skip"：跳过该文件
        overwrite_original: True 时直接返回源文件路径（就地覆盖原图）

    Returns:
        输出文件的完整绝对路径；
        **返回 None 表示该文件应被跳过**（on_conflict="skip" 且同名已存在）。

    注意 overwrite 模式下的"同批自撞"：
        两个源文件可能映射到同一个输出名（例如模板只含 {date}）。
        此时后一个会把前一个的结果覆盖掉，用户会莫名其妙少一张输出。
        因此同一批内的重名一律按重命名处理，只有「磁盘上已存在的旧文件」
        才会被真正覆盖。
    """
    if overwrite_original:
        # 就地覆盖：输出即源文件。写出方必须走原子替换，
        # 否则中途失败会把原图毁掉
        return src_path

    mode = on_conflict if on_conflict in CONFLICT_MODES else CONFLICT_RENAME

    # 确定输出目录
    if not output_dir:
        # 默认：在源文件所在目录下创建 watermarked_output 子文件夹
        output_dir = os.path.join(os.path.dirname(src_path), "watermarked_output")

    # 解析原文件信息
    src_dir, src_fullname = os.path.split(src_path)
    src_name, src_ext = os.path.splitext(src_fullname)

    # 确定输出扩展名
    if output_format == "auto":
        out_ext = src_ext.lower()
        if out_ext in (".jpg", ".jpeg"):
            out_ext = ".jpg"
        elif out_ext == ".tif":
            out_ext = ".tiff"
        else:
            out_ext = src_ext  # 保持原扩展名
    elif output_format == "jpeg":
        out_ext = ".jpg"
    else:
        out_ext = f".{output_format}"

    # 替换模板变量
    now = datetime.now()
    name = filename_template
    name = name.replace("{original}", src_name)
    name = name.replace("{date}", now.strftime("%Y%m%d"))
    name = name.replace("{time}", now.strftime("%H%M%S"))
    if seq_counter is not None and "{seq}" in name:
        name = name.replace("{seq}", str(seq_counter))
    name = name.strip() or src_name

    # 组合完整路径
    candidate = os.path.join(output_dir, f"{name}{out_ext}")

    # 重名检测
    def _taken(path):
        # 同一批内已用过的名字一定算占用（覆盖模式下也不能自己覆盖自己）
        if existing_names and path in existing_names:
            return True
        if mode == CONFLICT_OVERWRITE:
            return False  # 磁盘上的旧文件允许覆盖
        return os.path.exists(path)

    if _taken(candidate):
        if mode == CONFLICT_SKIP:
            return None
        counter = 1
        while True:
            alt = os.path.join(output_dir, f"{name}_{counter}{out_ext}")
            if not _taken(alt):
                candidate = alt
                break
            counter += 1

    # 登记到 existing_names（供后续文件检测）
    if existing_names is not None:
        existing_names.add(candidate)

    return candidate


def generate_merged_output_path(
    output_dir: str,
    existing_names: Optional[set[str]] = None,
    prefix: str = "合并水印",
) -> str:
    """
    合并输出（多合一 PDF）的文件路径。

    合并结果没有"某一个原始文件名"可用，因此用固定前缀 + 时间戳命名：
    既不会和逐文件输出撞名，也能一眼看出这是合并结果。

    Args:
        output_dir: 输出目录（空字符串时退回用户主目录）
        existing_names: 已存在的输出文件名集合（同批内避免自撞）
        prefix: 文件名前缀

    Returns:
        输出 PDF 的完整绝对路径
    """
    if not output_dir:
        output_dir = os.path.expanduser("~")

    now = datetime.now()
    stem = f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}"

    def _is_taken(path):
        if existing_names and path in existing_names:
            return True
        return os.path.exists(path)

    candidate = os.path.join(output_dir, f"{stem}.pdf")
    counter = 1
    while _is_taken(candidate):
        candidate = os.path.join(output_dir, f"{stem}_{counter}.pdf")
        counter += 1

    if existing_names is not None:
        existing_names.add(candidate)

    return candidate


def get_default_output_dir(src_files: list[str]) -> str:
    """
    获取默认输出目录。

    策略：如果所有源文件在同一目录下，则在该目录下创建 watermarked_output 子文件夹；
    否则返回空字符串（由 generate_output_path 自动处理 —— 每个文件落到自己的源目录下）。
    """
    if not src_files:
        return ""

    dirs = set(os.path.dirname(f) for f in src_files)
    if len(dirs) == 1:
        return os.path.join(dirs.pop(), "watermarked_output")
    return ""


def group_by_directory(files) -> dict[str, list[str]]:
    """按源文件所在目录分组（保持列表顺序）"""
    groups: dict[str, list[str]] = {}
    for path in files or []:
        groups.setdefault(os.path.dirname(path), []).append(path)
    return groups


# 多目录 + 合并输出时的兜底目录名（单个输出文件必须有唯一落点）
DESKTOP_FALLBACK_DIRNAME = "水印输出"


def resolve_output_target(files, chosen_dir: str = "", merge: bool = False) -> tuple[str, str]:
    """
    决定本批的输出目录，返回 (目录, 说明文案)。

    **目录为空字符串表示"按各源文件所在目录分别输出"** —— 这是关键：
    旧实现对「输出目录留空 + 多目录输入」会统一塞到桌面「水印输出」，
    而界面提示写的是「留空→原图目录\\watermarked_output」，两者完全不符，
    用户以为会各自落在原目录旁边，实际全跑到桌面上去了。

    规则（与界面提示逐条对应）：
      1. 用户选了目录            → 用它
      2. 单目录输入              → 该目录下 watermarked_output/
      3. 多目录 + 合并成单个 PDF → 桌面「水印输出」（单文件必须有唯一落点）
      4. 多目录 + 逐文件输出     → 留空，由 generate_output_path 各自落到源目录下
    """
    if chosen_dir and os.path.isdir(chosen_dir):
        return chosen_dir, ""

    groups = group_by_directory(files)
    if not groups:
        return "", ""

    if len(groups) == 1:
        only = next(iter(groups))
        return (
            os.path.join(only, "watermarked_output"),
            "输出目录留空 → 源文件目录下的 watermarked_output/",
        )

    if merge:
        return (
            os.path.join(os.path.expanduser("~"), "Desktop", DESKTOP_FALLBACK_DIRNAME),
            f"多个来源目录 + 合并为单个 PDF → 桌面「{DESKTOP_FALLBACK_DIRNAME}」",
        )

    return "", "输出目录留空 → 每个文件写到各自源目录下的 watermarked_output/"


# ============================================================
# 原子写出 —— 异常 / 中断不留半成品
# ============================================================

def make_temp_output_path(final_path: str) -> str:
    """
    同目录下的临时文件名（保留扩展名）。

    为什么必须保留扩展名：Pillow 的 save() 按扩展名决定编码格式，
    临时文件名若丢掉 .jpg / .png，写出的格式就会跑偏。
    """
    directory, name = os.path.split(final_path)
    stem, ext = os.path.splitext(name)
    return os.path.join(directory, f".{stem}.part{ext}")


def _silent_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


@contextlib.contextmanager
def atomic_output(final_path: str):
    """
    在临时文件里写完，再原子替换到 final_path。

    为什么必须这样：
    直接往最终路径写，一旦中途异常 / 用户取消 / 进程被杀，
    就会留下一个"看着存在、其实是坏文件"的结果 ——
    批量场景下这种半成品最难排查，用户打开才发现是坏的。
    os.replace 在同一文件系统内是原子操作：要么完整出现，要么完全不存在。

    用法：
        with atomic_output(target) as tmp:
            write_stuff(tmp)
    """
    tmp = make_temp_output_path(final_path)
    os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
    _silent_remove(tmp)
    try:
        yield tmp
    except BaseException:
        _silent_remove(tmp)   # 失败路径：绝不留残骸
        raise
    else:
        os.replace(tmp, final_path)
