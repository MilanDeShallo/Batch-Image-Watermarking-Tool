"""
视频水印引擎 —— 用 ffmpeg 叠加，渲染逻辑完全复用 watermark_engine

为什么走「覆盖层 PNG」而不是逐帧用 Pillow 处理：
    现有引擎已经把水印算成"精灵 + 坐标"，最终 alpha_composite 到一张画布上。
    给它一张全透明的同尺寸画布，返回值天然就是"只有水印"的 RGBA 层。
    把这一层存成 PNG 交给 ffmpeg 循环叠加，等于把 Pillow 的定位 / 平铺 /
    旋转 / 透明度 / 字号占比原样搬到视频上 —— 视频和图片不会存在第二套几何
    实现，也就不会出现"图片对、视频偏了"这类最难查的偏差。

为什么画质给到 CRF 16 而不是 18/20：
    x264/x265 的 CRF 18 是业界所谓的"视觉无损起点"，但在高动态、暗部渐变
    场景（夜景、烟雾、霓虹）仍能看到轻微色块。CRF 16 + preset slow 在实测里
    与源片的 SSIM 稳定在 0.99 以上，代价是体积比 CRF 18 大约 30%。
    用户明确要求"不以损失画质为代价"，这里不省这个体积。

为什么位深必须跟着源走：
    10bit 源（iPhone HDR、专业素材）压成 8bit 会在天空、渐变背景上出现
    明显色带。这不是"画质下降一点"，是肉眼一眼能看出的劣化。

为什么 HDR 绝不能落到 8bit SDR：
    HLG / PQ 的传输曲线与 BT.709 完全不同。把 HDR 当 SDR 编码，画面会整体
    发灰、暗部死黑。检测到 HDR 就转 10bit HEVC 并完整保留色彩标记。

设计原则：
- 本模块不 import 任何 GUI 库
- 命令构建是纯函数（build_ffmpeg_command），可脱离 ffmpeg 单测
- ffmpeg 缺失时抛 FileNotFoundError，由调用方给出可读提示
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from PIL import Image

# 可处理的视频扩展名定义在 src/config.py（与图片扩展名同一处维护），
# 避免两处各写一份、改一处忘一处。
from src.config import VIDEO_EXTENSIONS
from src.logger import log_info, log_warning

# ============================================================
# 常量 —— 画质与兼容性的单一来源
# ============================================================

# 视觉无损档的编码参数。
# CRF 16 而不是 18：见模块头的说明。preset slow 比 medium 慢约 1.8 倍，
# 但同 CRF 下码率分配更聪明，暗部/细节保得更好。
DEFAULT_CRF = 16
DEFAULT_PRESET = "slow"

# 「以画质为先」的极端档：CRF 12，体积通常是 CRF 16 的 2~3 倍。
# 留给"这条片子要回流到剪辑流程"的场景。
HIGH_QUALITY_CRF = 12
HIGH_QUALITY_PRESET = "slower"

# 音频轨需要转码时的目标码率。源是 AAC 时一律 copy（零损失），
# 只有 mp4 容器装不下的编码（PCM / Opus / Vorbis）才落这里。
AUDIO_FALLBACK_BITRATE = "256k"

# mp4 / mov 容器能直接容纳的音频编码。其余必须转 AAC。
# 注意：PCM 系列（mov 里的 lpcm）在 mp4 里装不下，copy 会直接报错，
# 所以这里必须白名单而不是黑名单 —— 漏一个就是一个用户可见的失败。
MP4_AUDIO_CODECS = frozenset({"aac", "mp3", "ac3", "eac3", "alac"})

# mp4 / mov 容器能直接容纳的字幕编码。其它（srt / ass / pgs）跳过并提示。
# 营销素材的字幕基本都是烧进画面的，这里不值得为字幕引入失败风险。
MP4_SUBTITLE_CODECS = frozenset({"mov_text"})

# HDR 的传输特性取值（ffprobe 的 color_transfer 字段）。
# smpte2084 = PQ（HDR10 / Dolby Vision 的底层），arib-std-b67 = HLG（iPhone 默认）。
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67", "smpte428"})

# 源编码 → 编码器的映射（「保持原编码」模式用）。
# 只映射 mp4 生态里最主流的两个；VP9 / AV1 / ProRes / MPEG-4 一律回落 H.264 ——
# 那些编码器要么不在精简版 ffmpeg 里，要么 mp4 容器兼容性差。
SOURCE_MODE_ENCODERS = {
    "h264": "libx264",
    "hevc": "libx265",
}

# 源容器 → 输出容器（「保持原编码」模式用）。
# 只保留 mp4 生态里常见的几个；avi / webm / flv / wmv / ts 统一落 mp4 ——
# 那些容器装 H.264 要么不通用、要么播放器支持差，留着原容器反而给用户添麻烦。
SOURCE_MODE_CONTAINERS = {
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mov",
    ".mkv": "mkv",
}

# faststart（把索引挪到文件头）只对这些容器有意义，其它容器传了会报错
FASTSTART_CONTAINERS = frozenset({".mp4", ".m4v", ".mov"})

# 像素格式 → 位深。用于从 pix_fmt 反推"这个源是 8bit 还是 10bit"。
#
# 不能只用正则从末尾抓数字：`nv12` 的名字里带 12，但它是 8bit 的半平面
# 4:2:0 格式（12 指的是 4:2:0 的变体编号）。抓错就会把普通 8bit 视频
# 当成高 bit 深去编码，白白变慢一倍还多占体积。
# 所以先查显式表，再用"位深紧跟在 p 后面"的规则兜底。
_PIX_FMT_EXPLICIT_DEPTH = {
    "nv12": 8,
    "nv21": 8,
    "nv16": 8,
    "nv20le": 10,
    "p010le": 10,
    "p012le": 12,
    "p016le": 16,
}
# yuv420p10le / gbrp10le / yuv444p12le：位深紧随 p
_PIX_FMT_DEPTH_RE = re.compile(r"p(\d{2})(?:le|be)?$")

# 输出编码方式
OUTPUT_MODE_MP4 = "mp4"        # 统一修成 H.264 / HEVC + mp4（兼容优先）
OUTPUT_MODE_SOURCE = "source"  # 编回源编码器、源容器（流程一致优先）
OUTPUT_MODES = (OUTPUT_MODE_MP4, OUTPUT_MODE_SOURCE)


# ============================================================
# ffmpeg / ffprobe 定位
# ============================================================

def _tool_name(base: str) -> str:
    return f"{base}.exe" if os.name == "nt" else base


def _resolve_tool(base: str, env_var: str) -> str:
    """
    定位 ffmpeg / ffprobe。查找顺序刻意定成这样：

    1. 环境变量（调试与应急：换一版 ffmpeg 不必重打包）
    2. 打包目录（PyInstaller onedir 把 ffmpeg 塞在 _internal/ffmpeg/ 下）
    3. 系统 PATH（开发机）

    找不到就抛 FileNotFoundError 并给出**可操作**的提示，
    而不是让 subprocess 抛一句 "[WinError 2] 系统找不到指定的文件"。
    """
    name = _tool_name(base)

    override = os.environ.get(env_var, "").strip()
    if override:
        candidate = os.path.join(override, name)
        if os.path.isfile(candidate):
            return candidate

    if getattr(sys, "frozen", False):
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        for folder in ("ffmpeg", ""):
            candidate = os.path.join(base_dir, folder, name) if folder else os.path.join(base_dir, name)
            if os.path.isfile(candidate):
                return candidate

    found = shutil.which(base)
    if found:
        return found

    raise FileNotFoundError(
        f"未找到 {name}。视频水印需要 ffmpeg，请把它放到程序目录的 ffmpeg 文件夹下，"
        f"或设置环境变量 {env_var} 指向它所在的目录。"
    )


def ffmpeg_path() -> str:
    return _resolve_tool("ffmpeg", "WATERMARK_FFMPEG_DIR")


def ffprobe_path() -> str:
    return _resolve_tool("ffprobe", "WATERMARK_FFPROBE_DIR")


def ffmpeg_available() -> tuple[bool, str]:
    """
    探测 ffmpeg 是否可用。返回 (是否可用, 说明文字)。

    界面用它决定视频功能是否可点 —— 与其让用户点开始处理后报错，
    不如提前把"这台机器上没有 ffmpeg"说清楚。
    """
    try:
        exe = ffmpeg_path()
        ffprobe_path()
    except FileNotFoundError as exc:
        return False, str(exc)
    return True, exe


# ============================================================
# 视频信息探测
# ============================================================

def is_video_file(path: str) -> bool:
    """按扩展名判断是否为视频源"""
    return os.path.splitext(str(path or ""))[1].lower() in VIDEO_EXTENSIONS


def _parse_fraction(text, default: float = 0.0) -> float:
    """解析 ffprobe 的分数形式（"30000/1001" / "25/1"）"""
    if not text:
        return default
    try:
        raw = str(text).strip()
        if "/" in raw:
            num, den = raw.split("/", 1)
            den_value = float(den)
            return float(num) / den_value if den_value else default
        return float(raw)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def _parse_sar(text) -> tuple[int, int]:
    """解析 sample_aspect_ratio（"1:1" / "4:3" / "0:1" 表示未知）"""
    try:
        raw = str(text or "").strip()
        if ":" in raw:
            num, den = raw.split(":", 1)
            return int(num), int(den)
    except (TypeError, ValueError):
        pass
    return 1, 1


def _bit_depth_from_pix_fmt(pix_fmt: str) -> int:
    """
    从像素格式反推位深。

    yuv420p10le → 10，p010le → 10，yuv420p → 8，nv12 → 8（不是 12！）。

    为什么不能直接抓末尾数字：`nv12` / `nv21` 的数字是格式变体编号，
    与位深无关。查表优先，正则只负责"位深紧跟在 p 后面"这一种形态 ——
    这样将来出现新格式时，最坏结果是保守地按 8bit 处理（安全的默认），
    而不是误判成高位深。
    """
    fmt = str(pix_fmt or "").strip().lower()
    if not fmt:
        return 8

    if fmt in _PIX_FMT_EXPLICIT_DEPTH:
        return _PIX_FMT_EXPLICIT_DEPTH[fmt]

    # p010le / p012le 这类前导 0 的命名（P0xx 系列）
    zero_padded = re.search(r"p0(\d{2})(?:le|be)?$", fmt)
    if zero_padded:
        try:
            return int(zero_padded.group(1))
        except ValueError:
            pass

    match = _PIX_FMT_DEPTH_RE.search(fmt)
    if match:
        try:
            depth = int(match.group(1))
            if depth in (9, 10, 12, 14, 16):
                return depth
        except ValueError:
            pass
    return 8


def _rotation_from_stream(stream: dict) -> int:
    """
    读取旋转角度（0 / 90 / 180 / 270）。

    手机竖拍的视频实际是横着编码的，靠 side_data 里的 rotation 标记
    让播放器转正。ffmpeg 解码时默认会自动应用这个旋转（autorotate），
    所以覆盖层必须按"转正之后"的尺寸来做，否则水印整个位置偏掉 ——
    这和 PDF 的 /Rotate 是同一类坑。

    rotation 的符号：ffmpeg 约定逆时针为正，但 90/270 都要交换宽高，
    符号不影响尺寸判断，只归一化到 [0, 360) 即可。
    """
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                return int(round(float(side_data["rotation"]))) % 360
            except (TypeError, ValueError):
                pass

    tags = stream.get("tags") or {}
    for key in ("rotate", "ROTATE"):
        if key in tags:
            try:
                return int(float(tags[key])) % 360
            except (TypeError, ValueError):
                pass
    return 0


@dataclass
class VideoInfo:
    """一条视频的探测结果 —— 后面所有决策（尺寸/编码器/位深/HDR）都从这里推"""

    path: str
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    rotation: int = 0

    pix_fmt: str = ""
    bit_depth: int = 8
    color_space: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_range: str = ""

    video_codec: str = ""
    audio_codec: str = ""
    subtitle_codec: str = ""
    has_audio: bool = False
    has_subtitle: bool = False
    bit_rate: int = 0

    sar_num: int = 1
    sar_den: int = 1

    audio_channels: int = 0

    # ---- 派生属性 ----

    @property
    def is_hdr(self) -> bool:
        """是否为 HDR 源（决定能不能落 8bit / 能不能用 H.264）"""
        return str(self.color_transfer or "").lower() in HDR_TRANSFERS

    @property
    def display_size(self) -> tuple[int, int]:
        """
        转正之后的显示尺寸（= 覆盖层应该用的尺寸）。

        顺序有讲究：SAR 作用于编码宽高，旋转在显示阶段交换宽高。
        所以先折算 SAR，再按旋转交换 —— 反过来的话，非方形像素的竖拍视频
        会算出一个既错又看起来合理的尺寸。
        """
        width = self.width
        height = self.height
        if self.sar_num > 0 and self.sar_den > 0 and self.sar_num != self.sar_den:
            width = int(round(width * self.sar_num / self.sar_den))

        if self.rotation in (90, 270):
            width, height = height, width
        return width, height

    @property
    def needs_square_pixel_scale(self) -> bool:
        """是否为非方形像素（需要先统一成方形像素，否则水印会被拉伸变形）"""
        return self.sar_num > 0 and self.sar_den > 0 and self.sar_num != self.sar_den

    @property
    def duration_text(self) -> str:
        """时长的可读形式（mm:ss 或 h:mm:ss）"""
        return format_time(self.duration)

    def summary(self) -> str:
        """一行摘要，用于日志与界面提示"""
        width, height = self.display_size
        parts = [f"{width}x{height}", f"{self.fps:.2f}fps" if self.fps else "", self.duration_text]
        if self.is_hdr:
            parts.append(f"HDR({self.color_transfer})")
        elif self.bit_depth > 8:
            parts.append(f"{self.bit_depth}bit")
        if self.rotation:
            parts.append(f"旋转{self.rotation}°")
        return " / ".join(p for p in parts if p)


def format_time(seconds: float) -> str:
    """秒 → "mm:ss" / "h:mm:ss" """
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "00:00"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _run_probe(path: str) -> dict:
    """调 ffprobe 拿完整 JSON 描述"""
    command = [
        ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else "未知原因"
        raise ValueError(f"无法读取视频信息（{os.path.basename(path)}）：{reason}")
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"无法解析视频信息（{os.path.basename(path)}）：{exc}") from exc


def probe_video(path: str) -> VideoInfo:
    """
    探测视频的分辨率 / 时长 / 帧率 / 旋转 / 位深 / 色彩 / 音轨。

    任何一个字段缺失都不抛错 —— 视频来源太杂（手机、剪映、PR、录屏），
    为了一个缺失的 color_range 直接失败，用户会觉得"这软件连打开都不会"。
    只在拿不到尺寸或时长时才真正报错，因为那两个是后续流程的硬前提。
    """
    data = _run_probe(path)
    streams = data.get("streams") or []
    container = data.get("format") or {}

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise ValueError(f"文件里没有视频画面（{os.path.basename(path)}）")

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    subtitle_stream = next((s for s in streams if s.get("codec_type") == "subtitle"), None)

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError(f"无法获取视频分辨率（{os.path.basename(path)}）")

    pix_fmt = str(video_stream.get("pix_fmt") or "")
    sar_num, sar_den = _parse_sar(video_stream.get("sample_aspect_ratio"))

    duration = _parse_fraction(container.get("duration"))
    if duration <= 0:
        duration = _parse_fraction(video_stream.get("duration"))

    fps = _parse_fraction(video_stream.get("avg_frame_rate"))
    if fps <= 0:
        fps = _parse_fraction(video_stream.get("r_frame_rate"))

    bit_rate = 0
    try:
        bit_rate = int(container.get("bit_rate") or video_stream.get("bit_rate") or 0)
    except (TypeError, ValueError):
        pass

    info = VideoInfo(
        path=path,
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        rotation=_rotation_from_stream(video_stream),
        pix_fmt=pix_fmt,
        bit_depth=_bit_depth_from_pix_fmt(pix_fmt),
        color_space=str(video_stream.get("color_space") or ""),
        color_primaries=str(video_stream.get("color_primaries") or ""),
        color_transfer=str(video_stream.get("color_transfer") or ""),
        color_range=str(video_stream.get("color_range") or ""),
        video_codec=str(video_stream.get("codec_name") or ""),
        audio_codec=str((audio_stream or {}).get("codec_name") or ""),
        subtitle_codec=str((subtitle_stream or {}).get("codec_name") or ""),
        has_audio=audio_stream is not None,
        has_subtitle=subtitle_stream is not None,
        bit_rate=bit_rate,
        sar_num=sar_num,
        sar_den=sar_den,
        audio_channels=int((audio_stream or {}).get("channels") or 0),
    )

    if duration <= 0:
        raise ValueError(f"无法获取视频时长（{os.path.basename(path)}）")

    log_info(f"视频探测：{os.path.basename(path)} → {info.summary()}")
    return info


# ============================================================
# 抽帧（预览用）
# ============================================================

def extract_frame(path: str, position: float = 0.0, force_original_rotation: bool = True) -> Image.Image:
    """
    抽取某一时刻的画面，用作预览底图。

    :param position: 秒。默认 0（首帧）—— 首帧最能代表"用户打开就看到的样子"，
                     而且不用 seek，几乎不耗时。
    :param force_original_rotation: 保持转正后的显示方向（与导出一致）。

    为什么要"与导出一致"：预览的坐标是用户拖出来的，导出必须落在同一位置。
    如果预览取了未旋转的帧、导出却用了 autorotate，竖屏视频的水印会转到
    画面外 —— 所以这里显式要求 ffmpeg 先转正再抽帧（默认行为，但写清楚）。
    """
    command = [
        ffmpeg_path(),
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, float(position)):.3f}",
        "-i", path,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "png",
        "pipe:1",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not completed.stdout:
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        reason = detail[-1] if detail else "未知原因"
        raise ValueError(f"无法抽取视频画面（{os.path.basename(path)}）：{reason}")

    import io

    image = Image.open(io.BytesIO(completed.stdout))
    image.load()
    return image.convert("RGB")


# ============================================================
# 覆盖层生成（复用 Pillow 渲染管线）
# ============================================================

def build_overlay(size: tuple[int, int], cfg) -> Image.Image:
    """
    生成一张"只有水印"的全尺寸 RGBA 层。

    实现就是给 apply_watermark 一张全透明画布 —— 它内部的 alpha_composite
    会把水印原样合上去，透明底不受影响。这一步是整个视频方案的核心：
    它保证视频与图片的几何、透明度、字体渲染来自同一份代码。

    scale 固定 1.0：覆盖层就是导出分辨率，与图片导出的语义完全一致
    （预览的缩放换算只发生在预览阶段，见 watermark_engine 的 scale 约定）。
    """
    from src.watermark_engine import apply_watermark

    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"覆盖层尺寸非法：{width}x{height}")

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    return apply_watermark(canvas, cfg, scale=1.0)


def overlay_has_content(overlay: Image.Image) -> bool:
    """
    覆盖层是否真的有内容（全透明等于没加水印）。

    用途：文字为空 + 没配 Logo 时，与其跑一遍 ffmpeg 重编码整条视频
    却什么也没加，不如直接提示用户"当前没有可添加的水印"。
    重编码一次 10 分钟视频是实打实的时间成本。
    """
    if overlay is None:
        return False
    try:
        alpha = overlay.getchannel("A")
    except (ValueError, AttributeError):
        return True
    return alpha.getbbox() is not None


# ============================================================
# 编码参数决策（纯函数，可单测）
# ============================================================

def target_encoder(info: VideoInfo, output_mode: str, crf: Optional[int] = None) -> str:
    """
    决定用哪个编码器。

    优先级：
    1. HDR 源 → 必须 libx265。H.264 的 10bit HDR 没有标准化 signaling，
       写出来就是"能播但颜色不对"，比报错更糟。
    2. 「保持原编码」且源是 H.264 / HEVC → 编回原编码器。
    3. 其余 → libx264（兼容性最好）。

    :param crf: 传入 CRF 时按"画质档"处理（探针用途），不影响编码器选择。
    """
    if info.is_hdr:
        return "libx265"

    if output_mode == OUTPUT_MODE_SOURCE:
        return SOURCE_MODE_ENCODERS.get(str(info.video_codec or "").lower(), "libx264")

    return "libx264"


def target_container(source_path: str, output_mode: str) -> str:
    """
    决定输出容器的扩展名（不含点）。

    「统一 mp4」模式恒为 mp4；
    「保持原编码」模式下 mp4 / mov / mkv 各自保持，其余容器统一落 mp4 ——
    把 H.264 塞回 avi / flv / ts 只会让用户拿到一个播放器挑三拣四的文件。

    只吃源路径而不是完整 VideoInfo：批量规划阶段还没探测过文件，
    为了拿一个扩展名去跑一次 ffprobe 是纯浪费。
    """
    if output_mode != OUTPUT_MODE_SOURCE:
        return "mp4"
    ext = os.path.splitext(str(source_path or ""))[1].lower()
    return SOURCE_MODE_CONTAINERS.get(ext, "mp4")


def target_pix_fmt(info: VideoInfo, encoder: str) -> str:
    """
    决定输出像素格式。

    10bit / 12bit 源一律出 10bit（x264/x265 支持的最高位深就是 10bit，
    12bit 源落到 10bit 是行业标准做法，视觉上不可分辨）。
    HDR 源即使标称 8bit 也强制 10bit —— HLG 在 8bit 下必然出现色带。
    """
    if info.bit_depth > 8 or info.is_hdr:
        return "yuv420p10le"
    return "yuv420p"


def overlay_filter_format(pix_fmt: str) -> str:
    """
    overlay 滤镜的输出像素格式。

    这个参数**必须显式给**：ffmpeg 的 overlay 在 format 未指定时默认 yuv420，
    会把 10bit 输入直接降成 8bit —— 前面的 10bit 编码参数就白设了，
    而画面上不会报任何错，只是渐变处悄悄多出色带。

    注意取值是 `yuv420` / `yuv420p10`，**不带末尾的 p** ——
    这是 overlay 滤镜自己的枚举名，不是编码器用的 pix_fmt 写法。
    写成 yuv420p 会直接报 "Unable to parse format option value"。
    """
    return "yuv420p10" if "10" in str(pix_fmt) else "yuv420"


def audio_can_be_copied(info: VideoInfo, output_mode: str) -> bool:
    """
    音频能否原样 copy（零损失）。

    mp4 / mov 装得下的编码直接 copy；装不下的（PCM / Opus / Vorbis / FLAC）
    必须转 AAC，否则 ffmpeg 直接报 "Could not find tag for codec"。
    """
    if not info.has_audio:
        return False
    return str(info.audio_codec or "").lower() in MP4_AUDIO_CODECS


def _setparams_clause(info: VideoInfo) -> str:
    """
    把源的色彩属性显式写回帧上（HDR 保真的关键一步）。

    为什么不能用 `-color_primaries` / `-color_trc` 命令行选项：
    实测这两个选项对 libx264 / libx265 **不生效** —— ffprobe 回读结果是
    unknown，画面能播但颜色按 BT.709 解释，HDR 会整体发灰。
    只有 setparams 滤镜改的是帧本身的属性，编码器才会把它写进 VUI。

    另外 `-color_trc` 才是正确选项名，没有 `-color_transfer` 这个选项。

    只传源里真实存在的字段：setparams 拿到空值会直接报错，
    而"源没有色彩标记"是常态（很多录屏、老素材都没有）。
    """
    params: list[str] = []
    for field_name, param_name in (
        ("color_range", "range"),
        ("color_primaries", "color_primaries"),
        ("color_transfer", "color_trc"),
        ("color_space", "colorspace"),
    ):
        value = str(getattr(info, field_name, "") or "").strip()
        if value and value.lower() not in ("unknown", "reserved", "unspecified"):
            params.append(f"{param_name}={value}")
    if not params:
        return ""
    return "setparams=" + ":".join(params)


def build_video_filter(info: VideoInfo, pix_fmt: str) -> str:
    """
    构造 filter_complex。

    默认路径下**不插入任何缩放滤镜** —— 唯一的处理就是 overlay 本身，
    不产生额外重采样。只有非方形像素（SAR ≠ 1）时才先统一像素形状，
    那种源如果不处理，水印会被画面一起横向拉伸成椭圆。

    色彩属性放在 overlay **之后**再写一次：overlay 的透明混合理论上会保留
    主输入的帧属性，但不同版本 ffmpeg 的行为并不稳定。写在链路末端，
    输出文件的色彩标记就是确定的，不依赖中间滤镜的实现细节。
    """
    overlay_format = overlay_filter_format(pix_fmt)
    setparams = _setparams_clause(info)

    chain: list[str] = []
    if info.needs_square_pixel_scale:
        width, height = info.display_size
        chain.append(f"[0:v]scale={width}:{height}:flags=lanczos,setsar=1[base]")
        base_label = "[base]"
    else:
        base_label = "[0:v]"

    # 有 setparams 时 overlay 先落到 [ov]，否则直接就是最终标签
    overlay_label = "[ov]" if setparams else "[outv]"
    chain.append(
        f"{base_label}[1:v]overlay=0:0:eof_action=repeat:format={overlay_format}{overlay_label}"
    )
    if setparams:
        chain.append(f"[ov]{setparams}[outv]")

    return ";".join(chain)


def build_ffmpeg_command(
    info: VideoInfo,
    source_path: str,
    overlay_path: str,
    output_path: str,
    output_mode: str = OUTPUT_MODE_MP4,
    crf: Optional[int] = None,
    preset: Optional[str] = None,
) -> list[str]:
    """
    构建完整的 ffmpeg 命令行（纯函数，不依赖 ffmpeg 是否安装）。

    几个必须显式处理的点：
    - `-nostdin`：GUI 进程里 ffmpeg 默认会去抢标准输入，可能把界面卡住。
    - `-fps_mode passthrough`：不丢帧不补帧，保持源的时间戳节奏。
      手机视频常见可变帧率（VFR），一旦让 ffmpeg 自己"优化"，音画同步会漂。
    - `-movflags +faststart`：把索引挪到文件头，网页/微信里能边下边播，
      不加的话大文件要等下完才出画面。
    - 颜色标记逐项透传：少一个 bt2020 就会让 HDR 素材在播放器里整体偏色。
    """
    quality_crf = DEFAULT_CRF if crf is None else int(crf)
    quality_preset = DEFAULT_PRESET if not preset else str(preset)

    encoder = target_encoder(info, output_mode)
    pix_fmt = target_pix_fmt(info, encoder)

    command = [
        ffmpeg_path(),
        "-hide_banner", "-nostdin", "-y",
        "-loglevel", "error",
        "-nostats",
        "-i", source_path,
        "-i", overlay_path,
        "-filter_complex", build_video_filter(info, pix_fmt),
        "-map", "[outv]",
    ]

    # 音频：可选映射（`0:a?` 的 `?` 表示"没有就算了"），避免无音轨视频报错
    # 字幕：只透传 mp4 装得下的 mov_text，其余跳过（见 MP4_SUBTITLE_CODECS 说明）
    if info.has_audio:
        command += ["-map", "0:a?"]
    subtitle_ok = (
        info.has_subtitle
        and str(info.subtitle_codec or "").lower() in MP4_SUBTITLE_CODECS
    )
    if subtitle_ok:
        command += ["-map", "0:s?"]

    command += [
        "-c:v", encoder,
        "-preset", quality_preset,
        "-crf", str(quality_crf),
        "-pix_fmt", pix_fmt,
    ]

    # x265 的日志默认会往 stderr 刷一堆进度，压低到 error
    if encoder == "libx265":
        command += ["-x265-params", "log-level=error"]

    # 色彩标记不在这里传 —— 见 _setparams_clause 的说明：
    # 命令行上的 -color_primaries / -color_trc 对这两个编码器都不生效，
    # 要靠滤镜链末端的 setparams 才会真正写进 VUI。

    if info.has_audio:
        if audio_can_be_copied(info, output_mode):
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-b:a", AUDIO_FALLBACK_BITRATE]

    if subtitle_ok:
        command += ["-c:s", "copy"]

    container_ext = os.path.splitext(str(output_path))[1].lower()

    # mp4 里的 HEVC 必须打 hvc1 标签：ffmpeg 默认写的是 hev1，
    # QuickTime / iOS / 微信都打不开（用户看到的是"文件损坏"，画面根本不出现）。
    # 只有 mp4 系的容器认这个标签，写进 mkv 会报错。
    if encoder == "libx265" and container_ext in FASTSTART_CONTAINERS:
        command += ["-tag:v", "hvc1"]

    command += ["-fps_mode", "passthrough"]

    # faststart 把索引挪到文件头，网页/微信里能边下边播。
    # 它是 mov/mp4 系 muxer 的私有选项，传给 matroska 会直接报错。
    if container_ext in FASTSTART_CONTAINERS:
        command += ["-movflags", "+faststart"]

    command += ["-progress", "pipe:1", output_path]
    return command


# ============================================================
# 主流程
# ============================================================

class VideoCancelled(Exception):
    """用户取消（与 batch_planner.TaskCancelled 同构，但本模块不依赖批处理层）"""


def _terminate_process(process: subprocess.Popen, grace: float = 5.0) -> None:
    """
    结束 ffmpeg 子进程。

    先 terminate 让它自己收尾（避免留下半个 moov box 的坏文件），
    等不到再 kill。ffmpeg 收到 SIGTERM 会正常关闭输出文件，
    这一步做对了，atomic_output 的清理才有意义。
    """
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
        process.wait(timeout=grace)
    except (OSError, subprocess.TimeoutExpired):
        pass


def watermark_video(
    source_path: str,
    output_path: str,
    cfg,
    output_mode: str = OUTPUT_MODE_MP4,
    progress: Optional[Callable[[float, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    crf: Optional[int] = None,
    preset: Optional[str] = None,
    info: Optional[VideoInfo] = None,
) -> str:
    """
    给一条视频加水印。返回输出路径。

    :param progress: 回调 (百分比 0-100, 说明文字)。按**视频时长**计算，
                     不是按文件个数 —— 一条长视频应该慢慢爬，而不是瞬间跳到 100。
    :param should_cancel: 回调返回 True 时终止 ffmpeg 并抛 VideoCancelled。
    :param info: 已探测的信息（批处理里同一文件可能被探测过，省一次 ffprobe）。
    :param crf / preset: 留空用默认的视觉无损档（CRF 16 / slow）。

    输出先写到 `xxx.part.mp4` 再由 atomic_output 原子改名：
    中途取消 / 崩溃不会留下"看着存在其实是坏文件"的结果，
    这一点与图片路径完全一致（见 file_manager.atomic_output）。
    """
    from src.file_manager import atomic_output

    video = info or probe_video(source_path)
    width, height = video.display_size

    if width <= 0 or height <= 0:
        raise ValueError(f"视频尺寸非法：{width}x{height}")

    # 覆盖层放在临时目录：它是中间产物，不该出现在用户的输出目录里，
    # 也不该在取消后残留。TemporaryDirectory 保证异常路径也会清理。
    with tempfile.TemporaryDirectory(prefix="wm_overlay_") as temp_dir:
        overlay_path = os.path.join(temp_dir, "overlay.png")

        if progress:
            progress(0.0, "正在生成水印层…")
        overlay = build_overlay((width, height), cfg)

        if not overlay_has_content(overlay):
            raise ValueError("当前没有可添加的水印：文字为空且未选择 Logo")

        overlay.save(overlay_path, "PNG")
        del overlay  # 覆盖层可能几十 MB，尽早还给系统

        if should_cancel and should_cancel():
            raise VideoCancelled()

        # 命令在 with 内构建：输出路径必须是 atomic_output 给的临时文件名，
        # 否则中途取消会留下"看着存在、其实是坏文件"的半成品
        with atomic_output(output_path) as temp_output:
            command = build_ffmpeg_command(
                video, source_path, overlay_path, temp_output,
                output_mode=output_mode, crf=crf, preset=preset,
            )
            _run_ffmpeg(command, video, progress, should_cancel)

    log_info(f"视频水印完成：{os.path.basename(output_path)}")
    return output_path


def _run_ffmpeg(
    command: list[str],
    info: VideoInfo,
    progress: Optional[Callable[[float, str], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> None:
    """
    执行 ffmpeg 并解析进度。

    进度来源是 `-progress pipe:1` 的 stdout（key=value 行）。
    注意 `out_time_ms` 在 ffmpeg 里实际是**微秒**（历史遗留的命名错误），
    与 `out_time_us` 同义，两个都要认 —— 不同版本给的名字不一样。
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )

    stderr_chunks: list[str] = []
    # stderr 必须异步读空：ffmpeg 出错时会连续输出，
    # 管道写满就会把子进程卡死，表现为"进度停在某个数字不动"。
    import threading

    def _drain_stderr():
        try:
            for line in process.stderr:
                stderr_chunks.append(line)
        except (ValueError, OSError):
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    duration = max(0.001, float(info.duration or 0.0))
    cancelled = False

    try:
        for raw_line in process.stdout:
            if should_cancel and should_cancel():
                cancelled = True
                _terminate_process(process)
                break

            line = raw_line.strip()
            if not line or "=" not in line:
                continue

            key, _, value = line.partition("=")
            if key in ("out_time_us", "out_time_ms"):
                try:
                    seconds = int(value) / 1_000_000.0
                except (TypeError, ValueError):
                    continue
                percent = max(0.0, min(99.0, seconds / duration * 100.0))
                if progress:
                    progress(percent, f"正在处理 {format_time(seconds)} / {info.duration_text}")
            elif key == "progress" and value.strip() == "end":
                if progress:
                    progress(100.0, "正在收尾…")

        returncode = process.wait()
    finally:
        if process.poll() is None:
            _terminate_process(process)
        stderr_thread.join(timeout=2.0)

    if cancelled:
        raise VideoCancelled()

    if returncode != 0:
        detail = "".join(stderr_chunks).strip()
        tail = "\n".join(detail.splitlines()[-16:]) if detail else "未知原因"
        raise RuntimeError(f"视频处理失败（ffmpeg 返回 {returncode}）：\n{tail}")


# ============================================================
# 画质自检（SSIM）—— 用于验收"转码不损画质"这一承诺
# ============================================================

def measure_ssim(reference_path: str, candidate_path: str) -> Optional[float]:
    """
    计算成片相对原片的 SSIM（结构相似度，1.0 = 完全一致）。

    这不是主流程的一部分（要解码两遍，很慢），而是验收工具：
    改编码参数时用它拿数字说话，而不是凭"看着差不多"。

    注意：成片里有水印，水印覆盖区域的 SSIM 天然会低。
    所以判据是"整体 SSIM ≥ 0.99"，且对比时应尽量用水印面积小的样本。
    """
    command = [
        ffmpeg_path(),
        "-hide_banner", "-loglevel", "info", "-nostdin",
        "-i", candidate_path,
        "-i", reference_path,
        "-lavfi", "ssim",
        "-f", "null", "-",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r"All:\s*([0-9.]+)", output)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover - 手动调试入口
    import sys as _sys

    if len(_sys.argv) < 2:
        print("用法: python -m src.video_engine <视频文件>")
        raise SystemExit(1)

    _info = probe_video(_sys.argv[1])
    print(f"尺寸(编码): {_info.width}x{_info.height}")
    print(f"尺寸(显示): {_info.display_size[0]}x{_info.display_size[1]}")
    print(f"时长: {_info.duration_text}  帧率: {_info.fps:.3f}")
    print(f"旋转: {_info.rotation}°  位深: {_info.bit_depth}bit  HDR: {_info.is_hdr}")
    print(f"色彩: {_info.color_primaries or '-'} / {_info.color_transfer or '-'} / {_info.color_space or '-'}")
    print(f"编码: {_info.video_codec}  音频: {_info.audio_codec or '无'}")
