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

from src.config import (
    CONFLICT_RENAME,
    DEFAULT_FILENAME_TEMPLATE,
    WatermarkConfig,
)
from src.file_manager import DESKTOP_FALLBACK_DIRNAME, is_pdf_file
from src.video_engine import OUTPUT_MODE_MP4, is_video_file, target_container


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
    def is_video_source(self) -> bool:
        """
        是否视频源。

        和 is_pdf_source 一样，是"走哪条引擎分支"的开关：
        PDF → pypdf 覆盖层合并，图片 → Pillow，视频 → ffmpeg overlay。
        output_format 对它只是"输出容器扩展名"，不参与格式转换决策。
        """
        return is_video_file(self.path)

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
    has_video: bool = False  # 本批是否含视频（界面据此提示"合并已停用"等）

    @property
    def outputs_pdf(self) -> bool:
        """本次是否会产生 PDF 输出（合并模式，或逐文件输出 PDF）"""
        return self.merge or self.out_format == "pdf"

    @property
    def count(self) -> int:
        return len(self.tasks)

    def merge_items(self) -> list:
        """
        合并输入只取能进 PDF 的源。

        视频永远不进合并 —— 它没有"页"的概念，混进去只会让
        build_merged_pdf 在打开文件时就失败。
        """
        return [t.to_merge_item() for t in self.tasks if not t.is_video_source]


def plan_output_paths(
    plan: "BatchPlan",
    output_dir: str,
    filename_template: str = DEFAULT_FILENAME_TEMPLATE,
    on_conflict: str = CONFLICT_RENAME,
    overwrite_original: bool = False,
) -> tuple[list, str]:
    """
    为整批任务分配输出路径（纯逻辑，可单测）。

    :param plan: plan_batch 产出的计划
    :param output_dir: 输出目录；空字符串表示"按各源文件所在目录分别输出"
    :param filename_template: 文件名模板，支持 {original}/{date}/{time}/{seq}
    :param on_conflict: 同名冲突策略 rename / overwrite / skip
    :param overwrite_original: 就地覆盖原图（此时输出路径就是源路径）
    :return: (与 plan.tasks 一一对应的路径列表, 合并输出路径)
            列表元素为 None 表示该任务应被跳过。

    为什么要单独成函数：
    「哪个文件写到哪个名字」是导出方案的一部分，以前这段循环埋在
    AppWindow._on_start_processing 里，测试根本碰不到 ——
    于是模板参数写了、冲突策略实现了，却因为没人传进去而长期失效。
    """
    from src.file_manager import generate_merged_output_path, generate_output_path

    existing_names: set[str] = set()

    if plan.merge:
        merged_dir = output_dir or os.path.join(
            os.path.expanduser("~"), "Desktop", DESKTOP_FALLBACK_DIRNAME
        )
        return [], generate_merged_output_path(merged_dir, existing_names)

    paths: list = []
    warned_video_overwrite = False
    for index, task in enumerate(plan.tasks, start=1):
        # 视频永不就地覆盖：一次编码失败就把原始素材毁了，
        # 而视频素材往往没有第二份。这里在规划层强制关掉，
        # 界面即使漏了拦截也伤不到源文件。
        is_video = getattr(task, "is_video_source", False)
        if is_video and overwrite_original and not warned_video_overwrite:
            from src.logger import log_warning
            log_warning("视频不支持就地覆盖原文件，已改为输出到独立文件")
            warned_video_overwrite = True
        effective_overwrite = overwrite_original and not is_video

        path = generate_output_path(
            task.path,
            output_dir,
            filename_template=filename_template,
            output_format=task.output_format,
            seq_counter=index,
            existing_names=existing_names,
            on_conflict=on_conflict,
            overwrite_original=effective_overwrite,
        )
        if path and effective_overwrite:
            # 就地覆盖模式下 generate_output_path 直接返回源路径，
            # 这里补登记，避免同一批里两个任务落到同一个源文件上
            existing_names.add(path)
        paths.append(path)

    return paths, ""


def plan_batch(
    files,
    scopes,
    out_format: str = "auto",
    merge_pdf: bool = False,
    video_output_mode: str = OUTPUT_MODE_MP4,
) -> BatchPlan:
    """
    生成整批处理计划。

    :param files: 源文件路径列表（顺序即输出顺序）
    :param scopes: ScopeStore，提供「全局默认 → 文件层 → 页层」的参数解析
    :param out_format: 期望的输出格式（界面文案或内部标识都可）
    :param merge_pdf: 是否合并为单个 PDF（仅在 out_format 为 pdf 时生效）
    :param video_output_mode: 视频输出编码方式（mp4 / source），只影响视频源
    :return: BatchPlan

    说明：
    - PDF 源文件无论输出格式选什么，都强制输出 PDF
      （PNG/JPEG 选项对 PDF 源无效，把 PDF 页面光栅化会丢文字可搜性）
    - 视频源同理，output_format 一律翻译成**容器扩展名**（mp4 / mov / mkv）。
      它的"输出格式"下拉（PNG/JPEG/PDF）对视频没有任何意义 ——
      视频不可能变成 PNG，也不可能塞进合并 PDF。
    - 只要本批含视频，合并就自动停用：视频没有"页"，混进合并结果
      只会在打开文件时失败。界面通过 has_video 给用户明确提示。
    - page_configs 只收录被单独设置过的页；未收录的页沿用文件层/全局默认
    """
    norm = normalize_format(out_format)
    has_video = any(is_video_file(path) for path in files)
    merge = bool(merge_pdf) and norm == "pdf" and not has_video

    tasks = []
    for path in files:
        is_pdf = is_pdf_file(path)
        is_video = is_video_file(path)

        page_configs = {}
        if is_pdf:
            for page in scopes.page_numbers(path):
                page_configs[page] = scopes.config_for(path, page)

        if is_video:
            task_format = target_container(path, video_output_mode)
        elif is_pdf:
            task_format = "pdf"
        else:
            task_format = norm

        tasks.append(FileTask(
            path=path,
            config=scopes.config_for(path),
            output_format=task_format,
            page_configs=page_configs,
        ))

    return BatchPlan(tasks=tasks, merge=merge, out_format=norm, has_video=has_video)


class TaskCancelled(Exception):
    """用户在任务开始前取消了处理（尚未写出任何文件）"""


def execute_task(task: FileTask, output_path: str, jpeg_quality: int = 90,
                 should_cancel=None, overlay_scale: float | None = None,
                 video_output_mode: str = OUTPUT_MODE_MP4,
                 video_progress=None) -> int:
    """
    执行单个文件任务，结果写到 output_path。

    :param task: plan_batch 产出的单个任务
    :param output_path: 输出文件路径
    :param jpeg_quality: JPEG / PDF 内图片的编码质量
    :param should_cancel: 可选回调，返回 True 时抛 TaskCancelled
    :param overlay_scale: 覆盖层渲染倍率快照（PDF 源使用）；
        None 表示由 pdf_engine 取当时的全局值
    :param video_output_mode: 视频输出编码方式（mp4 / source）
    :param video_progress: 视频进度回调 (百分比, 文字)；图片/PDF 不使用
    :return: 写出的页数（图片、视频恒为 1）

    分支说明：
    - 视频源 → ffmpeg 叠加（覆盖层由 Pillow 引擎生成，几何与图片一致）
    - PDF 源 → 覆盖层合并，原文保留可选可搜，逐页参数生效
    - 图片 + 输出 pdf → 单页 PDF，页面尺寸按像素比例换算
    - 图片 + 输出 png/jpeg/auto → Pillow 引擎直接存图

    两个安全约定：
    1. **取消检查分两级**：入口检查一次（不再启动新的解码/渲染），
       PDF 源还会在**每页之间**再检查一次 —— 所以 800 页的 PDF 点取消能立刻停下，
       而不是把整本跑完。已经进入 Pillow 合成的那一页无法安全中断
       （合成过程没有中断点），但不会再开新的一页/一个新文件。
       视频源把同一个回调交给 ffmpeg 轮询，取消时直接终止子进程。
    2. **原子写出**：所有分支都先写临时文件、再 os.replace 到最终路径。
       中途异常 / 进程被杀都不会留下"看着存在、其实是坏文件"的半成品。
       覆盖原图模式尤其依赖这一点 —— 否则一次失败就能把源文件毁掉。
    """
    if should_cancel is not None and should_cancel():
        raise TaskCancelled("用户取消")

    from src.file_manager import atomic_output

    if task.is_video_source:
        from src.video_engine import VideoCancelled, watermark_video
        try:
            # atomic_output 由 watermark_video 内部处理：它必须把
            # ffmpeg 的输出直接指向临时文件，否则大文件要多拷一遍。
            return watermark_video(
                task.path,
                output_path,
                task.config,
                output_mode=video_output_mode,
                progress=video_progress,
                should_cancel=should_cancel,
            )
        except VideoCancelled as exc:
            raise TaskCancelled("用户取消") from exc

    if task.is_pdf_source:
        from src.pdf_engine import apply_pdf_watermark
        with atomic_output(output_path) as tmp:
            # should_cancel 必须透传：apply_pdf_watermark 在每页之间检查取消，
            # 漏掉这一个参数会让逐页取消在批处理路径永远不触发 ——
            # 机制写好了却没接上线，表现就是"点了取消没反应"。
            return apply_pdf_watermark(
                task.path, task.config, tmp, task.page_configs or None,
                should_cancel=should_cancel,
                overlay_scale=overlay_scale,
            )

    if task.output_format == "pdf":
        from src.pdf_engine import image_file_to_pdf_bytes
        data = image_file_to_pdf_bytes(task.path, task.config, jpeg_quality)
        with atomic_output(output_path) as tmp:
            with open(tmp, "wb") as f:
                f.write(data)
        return 1

    from src.watermark_engine import apply_watermark, safe_open_image, save_with_format

    img = safe_open_image(task.path)
    result = apply_watermark(img, task.config)
    with atomic_output(output_path) as tmp:
        save_with_format(result, tmp, jpeg_quality)
    # 显式释放，避免批量处理时大图堆积
    del img, result
    return 1
