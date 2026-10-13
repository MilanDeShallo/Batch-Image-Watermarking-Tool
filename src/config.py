"""
水印配置数据类 + JSON 序列化/反序列化

设计原则：
- WatermarkConfig 是 GUI 与引擎之间的单一数据契约
- to_dict / from_dict 支持预设系统持久化
- v4: from_dict 对 tuple 字段做 list -> tuple 类型恢复（JSON 不支持 tuple）
- v6: 默认值与换算口径集中在本模块顶部；文字不透明度以 opacity 为唯一权威源，
      构造时同步 color_rgba 的 alpha（见 __post_init__）
"""

import json
import os
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


# 需要从 JSON 反序列化时恢复为 tuple 的字段名
_TUPLE_FIELDS = {"color_rgba"}


# ============================================================
# 默认值与换算口径 —— 全项目唯一来源
# ============================================================
# 为什么集中在这里：
#   配置类、界面面板、README、命令行各自写一份默认值时，很容易出现
#   「配置写 Logo 缩放 10%，界面初值是 15%」这类不一致 ——
#   用户看到的和实际导出的根本不是同一套参数。
#   现在所有默认值只在这里定义，界面一律通过 WatermarkConfig() 取值。
DEFAULT_TEXT = ""                       # 默认不预填水印文字（避免意外批量水印）
DEFAULT_FONT_RATIO = 0.04               # 字号占图片短边比例
DEFAULT_MARGIN_PX = 20                  # 文字边距（像素）
DEFAULT_SPACING_PX = 100                # 平铺间距（像素）
DEFAULT_OPACITY = 50                    # 不透明度 0-100
DEFAULT_COLOR_RGBA = (255, 255, 255, 128)   # 仅 RGB 有意义，alpha 由 opacity 派生
DEFAULT_SCALE_RATIO = 0.10              # Logo 占底图宽度比例
DEFAULT_LOGO_MARGIN_PX = 20             # Logo 边距（像素）
DEFAULT_JPEG_QUALITY = 90
DEFAULT_FILENAME_TEMPLATE = "{original}_watermarked"

# 输出同名冲突策略
CONFLICT_RENAME = "rename"        # 自动重命名（默认，绝不覆盖已有文件）
CONFLICT_OVERWRITE = "overwrite"  # 覆盖同名输出文件
CONFLICT_SKIP = "skip"            # 跳过该文件
CONFLICT_MODES = (CONFLICT_RENAME, CONFLICT_OVERWRITE, CONFLICT_SKIP)
CONFLICT_LABELS = {
    CONFLICT_RENAME: "自动重命名",
    CONFLICT_OVERWRITE: "覆盖同名文件",
    CONFLICT_SKIP: "跳过",
}


def opacity_to_alpha(percent) -> int:
    """
    不透明度(0-100) → alpha(0-255)。

    这是**唯一**的换算口径：`color_rgba` 的第 4 位由它算出。
    以前面板与引擎各写一份（面板用 int() 截断、引擎直接读 alpha），
    于是出现「滑块显示 80%，实际按 128 渲染」的错位。
    """
    try:
        value = float(percent)
    except (TypeError, ValueError):
        value = float(DEFAULT_OPACITY)
    value = max(0.0, min(100.0, value))
    return int(round(value * 255 / 100.0))


def alpha_to_opacity(alpha) -> int:
    """alpha(0-255) → 不透明度(0-100)。仅用于把旧的 alpha 语义迁移过来。"""
    try:
        value = float(alpha)
    except (TypeError, ValueError):
        return DEFAULT_OPACITY
    value = max(0.0, min(255.0, value))
    return int(round(value * 100 / 255.0))


def opacity_from_color(rgba) -> int:
    """
    从 color_rgba 反推不透明度(0-100)。

    历史写法用 alpha 表达不透明度（如 color_rgba=(255,255,255,128)）。
    构造、反序列化、作用域补丁三个入口都调它翻译一次，
    内部就只剩 opacity 一个来源，历史数据也不会因为换了权威源而静默变淡。

    只给了三位 RGB 时视为完全不透明 —— 「只写颜色」的直觉就是实色。
    """
    try:
        parts = list(rgba)
    except TypeError:
        return DEFAULT_OPACITY
    if len(parts) < 4:
        return 100
    return alpha_to_opacity(parts[3])


@dataclass
class WatermarkConfig:
    """水印全部参数的数据容器"""

    # 水印类型
    watermark_type: str = "text"  # "text" | "image" | "both"

    # ---- 文字水印参数 ----
    text: str = DEFAULT_TEXT
    font_path: str = ""  # 空=自动探测系统字体
    font_ratio: float = DEFAULT_FONT_RATIO  # 字号相对图片短边百分比 (1%-20%)
    # color_rgba: 只有前三位（RGB）是输入，第 4 位由 opacity 派生。
    # 只想给颜色时可以直接写 color_rgba=(r, g, b)，不必管 alpha。
    color_rgba: tuple = DEFAULT_COLOR_RGBA
    # 不透明度 0-100 —— 文字透明度的唯一权威来源。
    # 默认 None 表示「未指定」：此时从 color_rgba 的第 4 位推断。
    # 这样老写法 WatermarkConfig(color_rgba=(r, g, b, 200)) 仍按 200 渲染，
    # 不会因为换了权威源而静默变淡；构造完成后该字段一定是具体整数。
    opacity: Optional[int] = None
    position: str = "bottom-right"  # 九宫格位置
    position_mode: str = "grid"  # "grid" | "custom"
    custom_x: int = 50  # 自定义位置 X 百分比 (0-100)
    custom_y: int = 50  # 自定义位置 Y 百分比 (0-100)
    margin: int = DEFAULT_MARGIN_PX  # 文字边距(px)
    angle: float = 0.0  # 旋转角度(-180~180)
    tile: bool = False  # 是否平铺
    spacing_x: int = DEFAULT_SPACING_PX  # 平铺水平间距
    spacing_y: int = DEFAULT_SPACING_PX  # 平铺垂直间距
    stagger: bool = False  # 平铺相邻行错位

    # ---- 图片水印参数 ----
    logo_path: str = ""  # Logo 文件路径
    scale_ratio: float = DEFAULT_SCALE_RATIO  # Logo 相对图片宽度比例 (1%-50%)
    logo_opacity: int = DEFAULT_OPACITY  # Logo 不透明度 0-100
    logo_tile: bool = False  # Logo 平铺
    logo_spacing_x: int = DEFAULT_SPACING_PX
    logo_spacing_y: int = DEFAULT_SPACING_PX
    logo_stagger: bool = False  # Logo 平铺相邻行错位（v5 新增，面板早有此控件）
    logo_angle: float = 0.0
    logo_position: str = "bottom-right"
    logo_position_mode: str = "grid"  # "grid" | "custom"
    logo_custom_x: int = 50
    logo_custom_y: int = 50
    logo_margin: int = DEFAULT_LOGO_MARGIN_PX  # Logo 边距(px)

    # ---- 输出参数 ----
    output_dir: str = ""  # 空=自动(源目录下 watermarked_output/)
    output_format: str = "auto"  # "auto" | "png" | "jpeg" | "pdf"
    jpeg_quality: int = DEFAULT_JPEG_QUALITY  # JPEG 质量 60-100
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    on_conflict: str = CONFLICT_RENAME  # 同名输出文件的处理策略
    overwrite_original: bool = False  # 就地覆盖原图（危险操作，界面需二次确认）

    # ---- 配置版本（用于自动迁移） ----
    version: int = 1

    # ============================================================
    # 派生值 —— 保证「不透明度」只有一个来源
    # ============================================================

    def __post_init__(self):
        """
        构造时就把 color_rgba 的 alpha 对齐到 opacity。

        为什么放在构造期而不是渲染期：
        只要配置对象在，alpha 与 opacity 就永远一致，
        面板回填、预设迁移、脚本手写配置都不可能再制造出
        「滑块显示 80%、实际按 128 渲染」的错位。
        """
        self.normalize_opacity()

    def resolved_opacity(self) -> int:
        """
        归一化后的不透明度(0-100)。

        opacity 未指定(None)时回落到 color_rgba 的第 4 位 —— 那是历史写法，
        在这里翻译完就不再参与渲染，对象内部只有 opacity 一个来源。
        """
        if self.opacity is None:
            return opacity_from_color(self.color_rgba)
        try:
            value = float(self.opacity)
        except (TypeError, ValueError):
            return DEFAULT_OPACITY
        return int(max(0.0, min(100.0, round(value))))

    def resolved_color_rgba(self) -> tuple:
        """文字水印实际使用的 RGBA（alpha 由 opacity 派生）"""
        rgba = self.color_rgba
        try:
            rgb = tuple(int(v) for v in list(rgba)[:3])
        except (TypeError, ValueError):
            rgb = DEFAULT_COLOR_RGBA[:3]
        while len(rgb) < 3:
            rgb = rgb + (255,)
        rgb = tuple(max(0, min(255, v)) for v in rgb)
        return (rgb[0], rgb[1], rgb[2], opacity_to_alpha(self.resolved_opacity()))

    def normalize_opacity(self) -> None:
        """
        把 opacity 与 color_rgba 的 alpha 对齐到同一个值。

        先定 opacity 再派生 alpha，顺序不能反 —— 否则会拿 alpha 又回推一次。
        """
        self.opacity = self.resolved_opacity()
        self.color_rgba = self.resolved_color_rgba()

    def text_alpha(self) -> int:
        """文字水印最终的 alpha（0-255）"""
        return opacity_to_alpha(self.resolved_opacity())

    def resolved_on_conflict(self) -> str:
        """归一化后的冲突策略，未知值回落到「自动重命名」"""
        value = str(self.on_conflict or "").strip().lower()
        return value if value in CONFLICT_MODES else CONFLICT_RENAME

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（用于 JSON 持久化）"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'WatermarkConfig':
        """
        从字典反序列化，支持版本迁移。

        - 忽略未知字段
        - tuple 字段自动从 list 恢复
        - 检测 version 字段，旧配置自动迁移
        """
        # 检测配置版本
        cfg_version = data.get("version", 0)

        # v0 → v1 迁移：旧配置无 version/position_mode/custom_x 等字段
        # from_dict 的未知字段过滤 + 默认值机制已自动处理

        # 获取所有合法字段名
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}

        processed = {}
        for k, v in data.items():
            if k not in valid_fields:
                continue  # 忽略未知字段
            if k in _TUPLE_FIELDS and isinstance(v, list):
                processed[k] = tuple(v)
            else:
                processed[k] = v

        # 确保版本号为最新
        processed["version"] = 1

        # 历史数据迁移：旧配置与旧脚本用 color_rgba 的第 4 位表达不透明度。
        # 这里不用显式搬运 —— opacity 字段缺省即 None，__post_init__ 会从
        # color_rgba 推断出来（见 resolved_opacity），旧预设因此不会静默变淡。
        return cls(**processed)

    def save_to_json(self, filepath: str) -> None:
        """保存配置到 JSON 文件"""
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load_from_json(cls, filepath: str) -> 'WatermarkConfig':
        """从 JSON 文件加载配置"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)


def load_preset_safe(filepath: str) -> tuple[WatermarkConfig, list[str]]:
    """
    安全加载预设文件。

    返回 (config, warnings) 元组：
    - warnings 为空表示无问题
    - warnings 非空表示有非致命问题（如 Logo 路径失效），需提示用户
    """
    config = WatermarkConfig.load_from_json(filepath)
    warnings = []

    # Logo 路径失效容错
    if config.watermark_type in ("image", "both") and config.logo_path:
        if not os.path.isfile(config.logo_path):
            warnings.append(f"预设绑定的 Logo 文件不存在：{config.logo_path}")
            config.logo_path = ""

    return config, warnings
