"""
输入值统一校验与归一化（无 GUI 依赖，可独立单元测试）

解决的问题：
1. 间距输入框绑定 IntVar 后，用户输入非数字或清空内容时 IntVar.get() 抛 TclError，
   异常被 _get_current_config 的 except 吞掉 → 配置变成 None，
   表现为预览停止刷新、点击导出无反应（静默失败）
2. 间距为 0 或负数时，tile_grid 的步长 sprite.width + spacing 可能 <= 0，
   range() 退化为空序列 → 水印静默不显示；步长恰为 0 时抛 ValueError
3. 自定义坐标、透明度、缩放等缺少上下界约束，越界值进入引擎后行为不可预期

对外接口：
- parse_number(raw, integer=False)      容错解析（None/空串/非数字/全角数字）
- clamp_number(raw, spec, ...)          解析 + 夹取到 [lo, hi]，失败回退默认值
- normalize_fields(data, specs)         按规格表批量校验，返回 (新字典, 修正说明列表)
"""

from __future__ import annotations

import math
from typing import Any

# ============================================================
# 字段规格表
# ============================================================
# 格式：字段名 -> (最小值, 最大值, 回退默认值, 是否整数)

# 文字水印面板
TEXT_FIELD_SPECS: dict[str, tuple[float, float, float, bool]] = {
    # 位置百分比允许小数：预览图上直接拖动水印时，整数百分比在
    # 3000px 宽的图上一步就是 30px，够不上"完全自定义"
    "custom_x":   (0.0,   100.0,   50.0,  False),  # 位置 X 百分比（小数可用）
    "custom_y":   (0.0,   100.0,   50.0,  False),  # 位置 Y 百分比（小数可用）
    "spacing_x":  (1.0, 10000.0,  100.0,  True),   # 水平间距（必须 > 0）
    "spacing_y":  (1.0, 10000.0,  100.0,  True),   # 垂直间距（必须 > 0）
    "margin":     (0.0,  1000.0,   20.0,  True),   # 边距
    "opacity":    (0.0,   100.0,   50.0,  True),   # 透明度
    "font_ratio": (0.005,   0.5,   0.04, False),   # 字号（占短边比例）
    "angle":      (-180.0, 180.0,   0.0, False),   # 旋转角度
}

# 图片（Logo）水印面板
IMAGE_FIELD_SPECS: dict[str, tuple[float, float, float, bool]] = {
    "logo_custom_x":  (0.0,   100.0,   50.0,  False),
    "logo_custom_y":  (0.0,   100.0,   50.0,  False),
    "logo_spacing_x": (1.0, 10000.0,  100.0,  True),
    "logo_spacing_y": (1.0, 10000.0,  100.0,  True),
    "logo_margin":    (0.0,  1000.0,    5.0,  True),
    "logo_opacity":   (0.0,   100.0,   50.0,  True),
    "scale_ratio":    (0.01,    1.0,   0.15, False),  # 缩放比例（小数）
    "logo_angle":     (-180.0, 180.0,    0.0, False),
}

# 修正提示中的字段中文名
_FIELD_LABELS: dict[str, str] = {
    "custom_x": "位置 X",
    "custom_y": "位置 Y",
    "spacing_x": "水平间距",
    "spacing_y": "垂直间距",
    "margin": "边距",
    "opacity": "透明度",
    "font_ratio": "字号",
    "angle": "旋转角度",
    "logo_custom_x": "Logo 位置 X",
    "logo_custom_y": "Logo 位置 Y",
    "logo_spacing_x": "Logo 水平间距",
    "logo_spacing_y": "Logo 垂直间距",
    "logo_margin": "Logo 边距",
    "logo_opacity": "Logo 透明度",
    "scale_ratio": "缩放比例",
    "logo_angle": "Logo 旋转角度",
}

# 全角字符 → 半角（中文输入法下常被误输入）
_FULLWIDTH_MAP = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "．": ".", "－": "-", "＋": "+", "　": " ",
})


# ============================================================
# 基础解析
# ============================================================

def parse_number(raw: Any, integer: bool = False) -> int | float | None:
    """
    容错解析数字。解析失败返回 None（不抛异常）。

    可接受的输入：
    - 数字类型（int / float）
    - 字符串数字："42"、"12.5"、"  30  "、"１２"（全角）
    拒绝的输入：
    - None、bool、空串、纯空白、非数字文本、"nan"、"inf"
    """
    if raw is None or isinstance(raw, bool):
        return None

    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).translate(_FULLWIDTH_MAP).strip()
        if not text:
            return None
        try:
            value = float(text)
        except (ValueError, TypeError):
            return None

    if math.isnan(value) or math.isinf(value):
        return None

    return int(value) if integer else value


# ============================================================
# 夹取
# ============================================================

def clamp_number(
    raw: Any,
    spec: tuple[float, float, float, bool],
) -> tuple[int | float, bool]:
    """
    按规格解析并夹取数值。

    :param raw: 原始输入（可能为 None / 空串 / 越界值 / 非数字）
    :param spec: (最小值, 最大值, 回退默认值, 是否整数)
    :return: (安全值, 是否发生了修正)
    """
    lo, hi, default, is_int = spec
    value = parse_number(raw, integer=is_int)

    if value is None:
        # 解析失败 → 回退默认值（属于修正，调用方应提示用户）
        return (int(default) if is_int else float(default)), True

    if value < lo:
        value = lo
        corrected = True
    elif value > hi:
        value = hi
        corrected = True
    else:
        corrected = False

    return (int(value) if is_int else float(value)), corrected


def _format_value(value: int | float) -> str:
    """
    修正提示里的数值格式化（整数不带小数点）。

    先 round 再格式化：拖动水印写回的是浮点百分比，直接 str() 会出现
    33.470000000000006 这种噪声值，提示里很难看。
    """
    if isinstance(value, float):
        value = round(value, 4)
        if value.is_integer():
            return str(int(value))
    return str(value)


def _describe(raw: Any) -> str:
    """修正提示里的原始值描述"""
    text = "" if raw is None else str(raw)
    text = text.strip()
    return f"“{text}”" if text else "空值"


# ============================================================
# 批量归一化
# ============================================================

def normalize_fields(
    data: dict,
    specs: dict[str, tuple[float, float, float, bool]],
) -> tuple[dict, list[str]]:
    """
    按规格表批量校验字段。

    规则：
    - 只校验 specs 中声明、且 data 中确实存在的键（缺失键不视为错误）
    - 每个字段先解析再夹取到 [lo, hi]，解析失败回退默认值
    - 未在 specs 中声明的字段原样保留，便于增量扩展

    :return: (归一化后的字典副本, 人类可读的修正说明列表)
    """
    result = dict(data)
    notices: list[str] = []

    for key, spec in specs.items():
        if key not in data:
            continue
        new_value, corrected = clamp_number(data[key], spec)
        result[key] = new_value
        if corrected:
            label = _FIELD_LABELS.get(key, key)
            notices.append(f"{label} {_describe(data[key])} → {_format_value(new_value)}")

    return result, notices


# ============================================================
# 引擎侧兜底
# ============================================================

def safe_step(size: int, spacing: int) -> int:
    """
    计算平铺步长，保证 > 0 且不会退化为逐像素粘贴。

    tile_grid 中步长 = 精灵尺寸 + 间距：
    - 步长为 0 → range() 抛 ValueError
    - 步长为负 → range() 返回空序列，水印静默消失

    兜底策略：步长 <= 0 时**退化为紧贴平铺**（步长 = 精灵尺寸）。
    注意不能兜底为 1 —— 那会变成逐像素粘贴，大图下直接卡死。
    """
    sprite_size = max(1, int(size))
    step = sprite_size + int(spacing)
    return step if step > 0 else sprite_size
