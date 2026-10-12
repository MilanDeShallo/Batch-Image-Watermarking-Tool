"""
批处理规划：把「文件列表 + 三层作用域参数 + 输出设置」翻译成可执行的任务清单。

为什么单独成模块
----------------
这里的决策（每个文件用哪套参数、每一页用哪套参数、输出什么格式、
要不要合并成一个 PDF）是纯逻辑，不含任何界面代码，
因此可以被测试直接覆盖 —— 不需要启动窗口，也不需要真实点击。

它同时是 GUI 与命令行/脚本的共同入口：只要给一个 ScopeStore，
就能算出完全一致的输出方案。
"""

import os
from dataclasses import dataclass, field

from src.config import WatermarkConfig
from src.file_manager import is_pdf_file


# 界面下拉文案 / 格式名 → 内部格式标识
FORMAT_ALIASES = {
    "自动": "auto",
    "auto": "auto",
    "PNG": "png",
    "png": "png",
    "JPEG": "jpeg",
    "jpeg": "jpeg",
    "JPG": "jpeg",
    "jpg": "jpeg",
    "PDF": "pdf",
    "pdf": "pdf",
}

# 归一化后的合法格式
VALID_FORMATS = ("auto", "png", "jpeg", "pdf")


def normalize_format(value: str) -> str:
    """把界面文案或格式名统一成内部标识，未知值回落到 auto"""
    if not value:
        return "auto"
    return FORMAT_ALIASES.get(str(value).strip(), "auto")


@dataclass
class FileTask:
    """单个文件的一次处理任务"""

    path: str
    config: WatermarkConfig
    output_format: str = "auto"  # 该文件实际输出格式（PDF 源恒为 "pdf"）
    page_configs: dict = field(default_factory=dict)  # {页码(0起): 配置}，仅 PDF

    @property
    def is_pdf_source(self) -> bool:
        return is_pdf_file(self.path)

    @property
    def has_page_configs(self) -> bool:
        return bool(self.page_configs)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def to_merge_item(self) -> dict:
        """转成 build_merged_pdf 需要的输入项"""
        return {
            "path": self.path,
            "config": self.config,
            "page_configs": dict(self.page_configs) or None,
        }


@dataclass
class BatchPlan:
    """整批的处理方案"""

    tasks: list = field(default_factory=list)
    merge: bool = False  # 是否合并为单个 PDF
    out_format: str = "auto"

    @property
    def outputs_pdf(self) -> bool:
        """本次是否会产生 PDF 输出（合并模式，或逐文件输出 PDF）"""
        return self.merge or self.out_format == "pdf"

    @property
    def count(self) -> int:
        return len(self.tasks)

    def merge_items(self) -> list:
        return [t.to_merge_item() for t in self.tasks]


def plan_batch(files, scopes, out_format: str = "auto", merge_pdf: bool = False) -> BatchPlan:
    """
    生成整批处理计划。

    :param files: 源文件路径列表（顺序即输出顺序）
    :param scopes: ScopeStore，提供「全局默认 → 文件层 → 页层」的参数解析
    :param out_format: 期望的输出格式（界面文案或内部标识都可）
    :param merge_pdf: 是否合并为单个 PDF（仅在 out_format 为 pdf 时生效）
    :return: BatchPlan

    说明：
    - PDF 源文件无论输出格式选什么，都强制输出 PDF
      （PNG/JPEG 选项对 PDF 源无效，把 PDF 页面光栅化会丢文字可搜性）
    - page_configs 只收录被单独设置过的页；未收录的页沿用文件层/全局默认
    """
    norm = normalize_format(out_format)
    merge = bool(merge_pdf) and norm == "pdf"

    tasks = []
    for path in files:
        is_pdf = is_pdf_file(path)

        page_configs = {}
        if is_pdf:
            for page in scopes.page_numbers(path):
                page_configs[page] = scopes.config_for(path, page)

        tasks.append(FileTask(
            path=path,
            config=scopes.config_for(path),
            output_format="pdf" if is_pdf else norm,
            page_configs=page_configs,
        ))

    return BatchPlan(tasks=tasks, merge=merge, out_format=norm)


def execute_task(task: FileTask, output_path: str, jpeg_quality: int = 90) -> int:
    """
    执行单个文件任务，结果写到 output_path。

    :param task: plan_batch 产出的单个任务
    :param output_path: 输出文件路径
    :param jpeg_quality: JPEG / PDF 内图片的编码质量
    :return: 写出的页数（图片恒为 1）

    分支说明：
    - PDF 源 → 覆盖层合并，原文保留可选可搜，逐页参数生效
    - 图片 + 输出 pdf → 单页 PDF，页面尺寸按像素比例换算
    - 图片 + 输出 png/jpeg/auto → Pillow 引擎直接存图
    """
    if task.is_pdf_source:
        from src.pdf_engine import apply_pdf_watermark
        return apply_pdf_watermark(
            task.path, task.config, output_path, task.page_configs or None
        )

    if task.output_format == "pdf":
        from src.pdf_engine import image_file_to_pdf_bytes
        data = image_file_to_pdf_bytes(task.path, task.config, jpeg_quality)
        with open(output_path, "wb") as f:
            f.write(data)
        return 1

    from src.watermark_engine import apply_watermark, safe_open_image, save_with_format

    img = safe_open_image(task.path)
    result = apply_watermark(img, task.config)
    save_with_format(result, output_path, jpeg_quality)
    # 显式释放，避免批量处理时大图堆积
    del img, result
    return 1
