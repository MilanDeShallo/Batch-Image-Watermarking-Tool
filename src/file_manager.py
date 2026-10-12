"""
文件管理器 — 文件收集、去重、格式校验、输出路径生成、路径清洗

职责：
- 从拖拽/文件对话框收集图片文件路径
- 扩展名过滤（仅保留 SUPPORTED_EXTENSIONS 中的格式）
- 去重（基于规范化后的绝对路径）
- 输出目录和输出文件名生成
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

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
    filename_template: str = "{original}_watermarked",
    output_format: str = "auto",
    seq_counter: Optional[int] = None,
    existing_names: Optional[set[str]] = None
) -> str:
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

    Returns:
        输出文件的完整绝对路径
    """
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

    # 组合完整路径
    candidate = os.path.join(output_dir, f"{name}{out_ext}")

    # 重名检测：检查 existing_names 集合 + 磁盘上是否已存在
    def _is_taken(path):
        if existing_names and path in existing_names:
            return True
        return os.path.exists(path)

    if _is_taken(candidate):
        counter = 1
        while True:
            candidate = os.path.join(output_dir, f"{name}_{counter}{out_ext}")
            if not _is_taken(candidate):
                break
            counter += 1

    # 登记到 existing_names（供后续文件检测）
    if existing_names is not None:
        existing_names.add(candidate)

    return candidate


def get_default_output_dir(src_files: list[str]) -> str:
    """
    获取默认输出目录。

    策略：如果所有源文件在同一目录下，则在该目录下创建 watermarked_output 子文件夹；
    否则返回空字符串（由 generate_output_path 自动处理）。
    """
    if not src_files:
        return ""

    dirs = set(os.path.dirname(f) for f in src_files)
    if len(dirs) == 1:
        return os.path.join(dirs.pop(), "watermarked_output")
    return ""
