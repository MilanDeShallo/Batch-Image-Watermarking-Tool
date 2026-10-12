"""
水印配置数据类 + JSON 序列化/反序列化

设计原则：
- WatermarkConfig 是 GUI 与引擎之间的单一数据契约
- to_dict / from_dict 支持预设系统持久化
- v4: from_dict 对 tuple 字段做 list -> tuple 类型恢复（JSON 不支持 tuple）
"""

import json
import os
from dataclasses import dataclass, asdict, field
from typing import Any


# 需要从 JSON 反序列化时恢复为 tuple 的字段名
_TUPLE_FIELDS = {"color_rgba"}


@dataclass
class WatermarkConfig:
    """水印全部参数的数据容器"""

    # 水印类型
    watermark_type: str = "text"  # "text" | "image" | "both"

    # ---- 文字水印参数 ----
    text: str = ""
    font_path: str = ""  # 空=自动探测系统字体
    font_ratio: float = 0.04  # 字号相对图片短边百分比 (1%-20%)
    color_rgba: tuple = (255, 255, 255, 128)  # RGBA
    opacity: int = 50  # 透明度 0-100
    position: str = "bottom-right"  # 九宫格位置
    position_mode: str = "grid"  # "grid" | "custom"
    custom_x: int = 50  # 自定义位置 X 百分比 (0-100)
    custom_y: int = 50  # 自定义位置 Y 百分比 (0-100)
    margin: int = 20  # 边距(px)
    angle: float = 0.0  # 旋转角度(-180~180)
    tile: bool = False  # 是否平铺
    spacing_x: int = 100  # 平铺水平间距
    spacing_y: int = 100  # 平铺垂直间距
    stagger: bool = False  # 平铺相邻行错位

    # ---- 图片水印参数 ----
    logo_path: str = ""  # Logo 文件路径
    scale_ratio: float = 0.10  # Logo 相对图片宽度比例 (1%-50%)
    logo_opacity: int = 50  # Logo 透明度 0-100
    logo_tile: bool = False  # Logo 平铺
    logo_spacing_x: int = 100
    logo_spacing_y: int = 100
    logo_stagger: bool = False  # Logo 平铺相邻行错位（v5 新增，面板早有此控件）
    logo_angle: float = 0.0
    logo_position: str = "bottom-right"
    logo_position_mode: str = "grid"  # "grid" | "custom"
    logo_custom_x: int = 50
    logo_custom_y: int = 50
    logo_margin: int = 20

    # ---- 输出参数 ----
    output_dir: str = ""  # 空=自动(源目录下 watermarked_output/)
    output_format: str = "auto"  # "auto" | "png" | "jpeg"
    jpeg_quality: int = 90  # JPEG 质量 60-100
    filename_template: str = "{original}_watermarked"
    overwrite_original: bool = False  # 是否覆盖原图

    # ---- 配置版本（用于自动迁移） ----
    version: int = 1

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
