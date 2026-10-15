"""
纯 Pillow 水印引擎（无 GUI 依赖，可独立测试/命令行调用）

核心功能：
1. safe_open_image() - 安全打开图片（EXIF 校正 + CMYK 兼容 + 大图安全）
2. prepare_exif_for_save() - 清除 Orientation 标记防二次旋转
3. add_text_watermark() - 文字水印（含空字符串容错 v4）
4. add_image_watermark() - 图片/Logo 水印
5. apply_watermark() - 统一入口，根据 config.type 分发
6. save_with_format() - 智能保存（格式转换 + 安全 EXIF）
7. load_font_with_fallback() - 字体四级回退链

设计原则：
- 本模块不 import 任何 GUI 库
- 所有函数接收 WatermarkConfig dataclass 作为参数
- 可通过 python -m src.watermark_engine 命令行测试
"""

from __future__ import annotations

import gc
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

from src.config import MAX_IMAGE_PIXELS, normalize_position
from src.logger import log_warning

# 合成管线是 8 位/通道，这些模式携带的位深无法保留 —— 转换时记一条日志，
# 避免「高光全白却毫无提示」。Pillow 对 16 位灰度会返回带 "I;" 前缀的名字
# （如 "I;16B"），所以按前缀匹配而不是写死集合。
_HIGH_DEPTH_MODES = {"I", "F"}
_HIGH_DEPTH_PREFIXES = ("I;", "F;")


def _is_high_depth_mode(mode: str) -> bool:
    """是否为 16/32 位灰度或浮点模式（转 8 位必然损失精度）"""
    return mode in _HIGH_DEPTH_MODES or mode.startswith(_HIGH_DEPTH_PREFIXES)

# ============================================================
# 全局安全设置
# ============================================================
# 图片像素上限。
#
# 历史实现把它设成 None（完全关掉 Pillow 的解压炸弹保护）—— 那等于放弃
# 最后一道防线：几十 KB 的畸形文件可以声称自己是 10 万×10 万，一 open 就要
# 40GB 内存，直接拖垮整个进程。现在恢复成**有限**的保护，只是把阈值提到
# 正常业务用不到的量级：
#   - 超过 3 亿像素        → Pillow 发警告（仍可处理）
#   - 超过 6 亿像素(2×)    → Pillow 抛 DecompressionBombError，
#                            safe_open_image 转成带文件名的明确错误，
#                            只失败这一个文件，不影响整批
#   - 超过 5000 万像素     → 批处理前由 resource_budget 给出内存预警，
#                            由用户决定是否继续（见 src/resource_budget.py）
# 数值来自 src/config.py（单一来源）。这里保留一个别名，
# 是因为本模块的错误消息与注释都按"上限（limit）"表述，读起来更直白。
MAX_IMAGE_PIXELS_LIMIT = MAX_IMAGE_PIXELS   # 约 1.2GB RGBA
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS_LIMIT

# EXIF Orientation 标签编号 (274 = 0x0112)
ORIENTATION_TAG = 0x0112

# 注意：这里曾经还有一份 SUPPORTED_EXTENSIONS，与 file_manager 的那份
# 内容不一致（少了 .pdf）且全项目无人引用 —— 已删除。
# 输入扩展名的唯一来源是 src/config.SUPPORTED_EXTENSIONS。


# ============================================================
# 按比例渲染 — 预览与导出一致的关键
# ============================================================
# 水印参数分两类：
#   1) 相对量：font_ratio（字号占短边比例）、scale_ratio（logo 占宽度比例）
#      —— 天然随图尺寸等比缩放，任何分辨率下渲染都一致
#   2) 绝对像素量：spacing_x/spacing_y、margin、logo_margin、logo_spacing_x/y
#      —— 只在「导出分辨率」下才是用户看到的那个距离
#
# 预览为了流畅会把图缩小后再渲染。若不换算第 2 类参数，预览里的水印
# 就会比真实导出更稀、离边更远（实测 6000px 图偏差 1.5 倍、8000px 偏 2 倍）。
#
# 约定：scale = 本次渲染分辨率 / 参考分辨率
#       参考分辨率 = 导出时使用的那套尺寸（图片=原图像素，PDF=页面可视尺寸×倍率）
#       scale = 1.0 表示按导出分辨率原样渲染，行为与历史版本完全一致
MIN_FONT_PX = 12   # 字号下限（参考分辨率下）
MIN_LOGO_PX = 16   # logo 宽度下限（参考分辨率下）


def scaled_px(value, scale: float = 1.0) -> int:
    """
    把「参考分辨率下的像素量」换算成本次渲染分辨率下的像素量。

    非数值输入回落到 0（由调用方的兜底逻辑处理），不抛异常 ——
    引擎是批量处理的最后一环，不能因为单个参数异常就整批失败。
    """
    try:
        return int(round(float(value) * float(scale)))
    except (TypeError, ValueError):
        return 0


# ============================================================
# 字体加载 — 四级回退链
# ============================================================

_FONT_FALLBACK_CHAIN = [
    lambda p: p if p else None,                    # 1. 用户指定路径
    lambda _: r"C:\Windows\Fonts\msyh.ttc",         # 2. 微软雅黑
    lambda _: r"C:\Windows\Fonts\simhei.ttf",       # 3. 黑体
]


def load_font_with_fallback(user_path: str | None, size: int) -> tuple:
    """
    加载字体文件，按优先级逐级回退。

    返回 (font, is_warning) 元组：
    - font: ImageFont.FreeTypeFont 或 ImageFont.ImageFont
    - is_warning: True 表示回退到了默认字体（不支持中文字符），调用方应提示用户
    """
    for getter in _FONT_FALLBACK_CHAIN:
        path = getter(user_path)
        if path and os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size), False
            except (OSError, IOError):
                continue
    # 全部失败 → Pillow 内置位图字体（不支持中文）
    return ImageFont.load_default(), True


# ============================================================
# 图片安全打开 — EXIF 校正 + 色彩空间兼容
# ============================================================

def safe_open_image(path: str) -> Image.Image:
    """
    安全打开图片文件，执行以下预处理：

    1. ImageOps.exif_transpose() - 自动校正手机/相机竖拍照片的旋转方向
    2. 色彩空间归一化 - CMYK/P/La 等非标准模式转为 RGB
    3. 解压炸弹保护 - 像素数超过 MAX_IMAGE_PIXELS_LIMIT 的文件被 Pillow 拒绝，
       这里转成带文件名的明确错误。否则整批只会看到一个陌生的异常类型，
       用户既不知道是哪个文件、也不知道为什么

    文件句柄释放（Windows 关键）：
    - Image.open() 返回惰性对象并持有文件句柄，只有 load()/convert() 之后才释放
    - 若在 open 与 load 之间抛异常，句柄会一直被占用，源图片无法重命名/删除
    - 因此用 with 包住整个读取过程，并返回完全脱离文件的对象副本
    """
    name = os.path.basename(path)

    try:
        opener = Image.open(path)
    except Image.DecompressionBombError as e:
        raise ValueError(
            f"图片像素数超过安全上限（{MAX_IMAGE_PIXELS_LIMIT:,} 像素），已拒绝处理：{name}"
        ) from e
    except Exception as e:
        # 文件头就损坏时 open() 阶段即失败（Pillow 抛 "Truncated File Read"），
        # 这一步同样要补文件名 —— 否则失败清单里只剩一个陌生的异常名，
        # 用户既不知道是哪个文件、也不知道该拿它怎么办
        raise ValueError(f"无法读取图片 {name}：{e}") from e

    # src.load() 必须包在异常处理里：损坏 / 截断的文件正是在这一步才暴露，
    # 而 Pillow 抛出的 "Truncated File Read" 不带文件名 ——
    # 批量失败清单里只会留下一句无从下手的报错，找不到是哪个文件出的问题
    try:
        with opener as src:
            src.load()  # 强制加载像素数据，尽早发现损坏文件

            # 1. EXIF 方向自动校正（返回脱离文件的新对象）
            try:
                detached = ImageOps.exif_transpose(src)
            except Exception:
                detached = None  # 某些图片可能无 EXIF 数据或解析失败，忽略

            # exif_transpose 未产生新对象时退化为副本，确保不持有句柄
            if detached is None or detached is src:
                detached = src.copy()
    except Exception as e:
        raise ValueError(f"无法读取图片 {name}：{e}") from e

    # 2. 色彩空间归一化
    #    管线最终在 RGBA 上合成，所以「模式名里不带 A、实际却含透明通道」的两种
    #    情况必须显式转 RGBA —— 一律 convert("RGB") 会把透明区域变成实心，
    #    LA 的半透明 Logo、带 transparency 的调色板 PNG 都会中招，
    #    导出的是一块黑底方块，而且没有任何报错。
    mode = detached.mode
    palette_alpha = mode == "P" and "transparency" in detached.info

    if mode == "LA" or palette_alpha:
        detached = detached.convert("RGBA")
    elif mode not in ("RGB", "RGBA", "L"):
        if _is_high_depth_mode(mode):
            log_warning(
                f"{name}：{mode} 为高位深/浮点模式，本工具按 8 位/通道合成，"
                f"位深会被压缩，高光细节可能丢失"
            )
        elif mode == "CMYK":
            log_warning(f"{name}：CMYK 将转换为 RGB（色彩空间转换不可逆）")
        detached = detached.convert("RGB")

    return detached


# ============================================================
# EXIF 保存 — 防二次旋转（v3 关键修复）
# ============================================================

def prepare_exif_for_save(img: Image.Image) -> Optional[bytes]:
    """
    清除 EXIF 中的 Orientation 标记为 1 (Normal)，防止二次旋转。

    背景：
    - safe_open_image() 中已调用 exif_transpose() 实际旋转了像素数据
    - 但原始 EXIF 中的 Orientation 仍为旧值（如 6=Rotate 90CW）
    - 如果原封不动写入新图，支持 EXIF 的查看器会对已旋转的图再次旋转 → 图片倒过来！

    返回：
    - bytes: 处理后的 EXIF 数据（可直接传给 img.save(exif=...)）
    - None: 无法获取/处理 EXIF 时返回 None（调用方应跳过 exif 参数）
    """
    try:
        exif_obj = img.getexif()
        exif_obj[ORIENTATION_TAG] = 1  # 1 = Normal, no rotation needed
        return exif_obj.tobytes()
    except Exception:
        return None


# ============================================================
# 位置计算 — 九宫格定位
# ============================================================

def anchor_position(
    base_size: tuple[int, int],
    sprite_size: tuple[int, int],
    position: str,
    margin: int
) -> tuple[int, int]:
    """
    根据九宫格位置字符串计算水印左上角坐标。

    position 格式："{vertical}-{horizontal}"
    - vertical: top / middle / bottom
    - horizontal: left / center / right
    特殊值："center" 等同于 "middle-center"
    历史别名（center-left / center-right）会先归一化为 middle-*
    """
    bw, bh = base_size
    sw, sh = sprite_size

    # 解析位置字符串
    # 先归一化别名：调用方可能来自旧预设、旧脚本或第三方代码，仍带着历史写法。
    # 不翻译就会落进下面 y 字典的 .get() 默认值 —— 表现为「选了左中、
    # 水印却渲染在正中」，而且不报错、界面按钮高亮也正常，很难被发现。
    position = normalize_position(position, "middle-center")
    if position == "center":
        position = "middle-center"

    parts = position.split("-", 1)
    v_pos = parts[0] if len(parts) >= 1 else "middle"
    h_pos = parts[1] if len(parts) >= 2 else "center"

    # 水平位置
    x = {
        "left": margin,
        "center": (bw - sw) // 2,
        "right": bw - sw - margin,
    }.get(h_pos, (bw - sw) // 2)

    # 垂直位置
    y = {
        "top": margin,
        "middle": (bh - sh) // 2,
        "bottom": bh - sh - margin,
    }.get(v_pos, (bh - sh) // 2)

    return (x, y)


def custom_anchor_position(
    base_size: tuple[int, int],
    sprite_size: tuple[int, int],
    x_pct: int,
    y_pct: int,
) -> tuple[int, int]:
    """
    根据自定义百分比坐标计算水印左上角坐标。

    Args:
        base_size: 基图尺寸 (width, height)
        sprite_size: 水印尺寸 (width, height)
        x_pct: 水平位置百分比 (0=左边缘, 100=右边缘)
        y_pct: 垂直位置百分比 (0=上边缘, 100=下边缘)

    Returns:
        (x, y) 水印左上角在基图上的像素坐标
    """
    bw, bh = base_size
    sw, sh = sprite_size

    # 将百分比转换为像素坐标（以水印中心为基准）
    x = int(bw * x_pct / 100) - sw // 2
    y = int(bh * y_pct / 100) - sh // 2

    # 边界约束：确保水印不超出图片范围
    x = max(0, min(x, bw - sw))
    y = max(0, min(y, bh - sh))

    return (x, y)


# ============================================================
# 平铺网格绘制
# ============================================================

# 判定「墨迹」的 alpha 阈值：低于此值视为透明（抗锯齿/插值边缘不计入尺寸）
INK_ALPHA_THRESHOLD = 8


def crop_to_ink(sprite: Image.Image, threshold: int = INK_ALPHA_THRESHOLD) -> Image.Image:
    """
    把精灵裁剪到实际可见内容的外框（去掉透明内边距）。

    为什么必须裁：
    - 文字精灵为容纳旋转后的四角、以及字体的 ascender/descender 空间，
      四周存在大量透明边距。实测 4% 字号下水平浪费约 39%、垂直浪费 64%~71%
    - 平铺步长 = 精灵尺寸 + 间距，透明边距会被算进「间距」里，
      于是间距调到 1 仍然看着很稀；单次定位时也会让边距/坐标虚高
    - 裁到墨迹外框后，间距与边距才等于用户眼睛看到的距离

    保留 1px 安全边避免抗锯齿边缘被切掉；无墨迹时原样返回（不做任何裁切）。
    """
    alpha = sprite.getchannel("A")
    bbox = alpha.point(lambda v: 255 if v >= threshold else 0).getbbox()
    if not bbox:
        return sprite

    left = max(0, bbox[0] - 1)
    top = max(0, bbox[1] - 1)
    right = min(sprite.width, bbox[2] + 1)
    bottom = min(sprite.height, bbox[3] + 1)
    if (left, top, right, bottom) == (0, 0, sprite.width, sprite.height):
        return sprite
    return sprite.crop((left, top, right, bottom))


def bleed_rgb_under_alpha(img: Image.Image) -> Image.Image:
    """
    把完全透明处的 RGB 填充为墨迹平均色，alpha 通道保持不变。

    为什么需要（PDF 覆盖层专用）：
    - 半透明图像的透明区域 RGB 通常是 (0,0,0)。PDF 阅读器在缩放显示时会对
      内嵌图像重采样，若采样发生在未预乘的数据上，黑色的透明底会被平均进
      笔画边缘，表现为水印边缘发灰发暗
      （实测：595pt 页面 100% 缩放下，白字水印出现 857 个灰边像素）
    - 把透明处的 RGB 换成墨迹色后，重采样结果与墨迹同色，边缘不再变暗
      （同条件实测灰边像素降为 0，红字水印的 R<250 暗边像素同样归零）
    - 黑色墨迹不受影响（本来就是黑），全透明图直接原样返回

    注意：只改 RGB，不动 alpha，因此不影响水印的不透明度与形状。
    """
    alpha = img.getchannel("A")
    if alpha.getextrema()[1] == 0:
        return img  # 全透明：没有墨迹可取样

    # 以墨迹区域（alpha 达标处）的平均色作为填充色
    ink_mask = alpha.point(lambda v: 255 if v >= INK_ALPHA_THRESHOLD else 0)
    stats = ImageStat.Stat(img, ink_mask)
    ink = tuple(int(round(v)) for v in stats.mean[:3])

    # 只把透明处换成墨迹色，可见处保留原色
    filled = Image.new("RGB", img.size, ink)
    filled.paste(img.convert("RGB"), mask=alpha)

    out = filled.convert("RGBA")
    out.putalpha(alpha)
    return out


def tile_grid(
    overlay: Image.Image,
    sprite: Image.Image,
    spacing_x: int,
    spacing_y: int,
    stagger: bool = False
) -> None:
    """
    在 overlay 上以平铺方式粘贴 sprite。

    Args:
        overlay: 目标画布（RGBA 模式，会被就地修改）
        sprite: 水印精灵图（应为 crop_to_ink 处理过的墨迹外框）
        spacing_x: 水平间距（相邻水印可见边缘之间的距离）
        spacing_y: 垂直间距
        stagger: 是否相邻行错位（半间距偏移）
    """
    # 步长兜底：sprite 尺寸 + 间距 有可能为 0 或负数
    # 0 → range() 抛 ValueError；负数 → range() 返回空序列，水印静默消失
    from src.validators import safe_step
    step_x = safe_step(sprite.width, spacing_x)
    step_y = safe_step(sprite.height, spacing_y)
    row = 0

    for y in range(-sprite.height, overlay.height + step_y, step_y):
        offset = (step_x // 2) if (row % 2 and stagger) else 0
        for x in range(-sprite.width + offset, overlay.width + step_x, step_x):
            # 必须用 alpha_composite，不能用 paste(sprite, pos, sprite)：
            # 拿 RGBA 精灵当遮罩时，Pillow 会把遮罩同时乘到 RGB 和 alpha 上，
            # 半透明水印的 alpha 被平方级削弱（实测 50% 不透明度只剩 1/4 浓度，
            # 且 RGB 被预乘导致"越半透明越灰"），滑块中段几乎失效
            overlay.alpha_composite(sprite, (x, y))
        row += 1


# ============================================================
# 文字水印渲染
# ============================================================

def _render_text_sprite(
    text: str,
    font: ImageFont.FreeTypeFont,
    color_rgba: tuple,
    angle: float
) -> Image.Image:
    """
    先在透明图层上渲染文字，再整体旋转，最后裁到墨迹外框。

    注意 bbox 负坐标处理：
    - 某些字体或含下沉字母(g/j/p/y)的文字，bbox[0] 或 bbox[1] 可能为负数
    - 绘制位置需用 (pad - bbox[0], pad - bbox[1]) 补偿
    - 画布尺寸也需相应调整

    内边距为什么只留 2px：
    - 旋转由 rotate(expand=True) 自动扩充画布，四角不会被裁掉；
      旧实现额外按宽度 30% 留内边距，这些透明边距会被平铺步长吃掉
      （实测水平浪费 39%、垂直浪费 64%~71%），
      正是"间距调到 1 仍然很稀"的根因
    - 2px 仅用于吸收双三次插值的振铃，随后 crop_to_ink 再裁一次
    """
    # 第一步：在 1x1 临时图上测量文字包围盒
    tmp = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    bbox = d.multiline_textbbox((0, 0), text, font=font, align="center")

    pad = 2  # 抗锯齿/插值安全边（旋转扩充由 expand=True 负责）

    # 创建实际尺寸的透明图层（Pillow 12.x bbox 返回 float，需转 int）
    layer_w = max(1, int(bbox[2] - bbox[0]) + pad * 2)
    layer_h = max(1, int(bbox[3] - bbox[1]) + pad * 2)
    layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))

    # 在补偿后的位置绘制文字
    draw = ImageDraw.Draw(layer)
    draw.multiline_text(
        (pad - bbox[0], pad - bbox[1]),
        text,
        font=font,
        fill=color_rgba,
        align="center"
    )

    # 旋转（如有角度）
    if angle:
        layer = layer.rotate(angle, expand=True, resample=Image.BICUBIC)

    # 裁到墨迹外框：间距与边距从此按"看得见的边缘"计算
    return crop_to_ink(layer)


def add_text_watermark(base: Image.Image, cfg, scale: float = 1.0) -> Image.Image:
    """
    为图片添加文字水印。

    v4 新增：空字符串/纯空格容错 —— 若 text 为空或纯空白，直接返回原图。

    :param scale: 本次渲染分辨率 / 参考分辨率（导出分辨率）。
                  平铺间距、边距、字号下限都按它换算，使预览与导出几何一致；
                  scale=1.0 即导出，与历史行为完全一致。
    """
    # v4: 空字符串/纯空格容错
    if not cfg.text or not cfg.text.strip():
        return base

    base = base.convert("RGBA")

    # 计算字号（相对短边的百分比）。
    # 字号下限也按 scale 换算：否则预览缩小时字号会被 12px 下限顶住，
    # 显示出的字号比导出大（ratio 很小时能差 3 倍以上）
    font_floor = max(1, scaled_px(MIN_FONT_PX, scale))
    font_size = max(font_floor, int(min(base.size) * cfg.font_ratio))
    font, is_warning = load_font_with_fallback(cfg.font_path or None, font_size)

    # 渲染文字 sprite
    # 不透明度只有一个来源：opacity。这里取派生出的 alpha，
    # 而不是直接用 cfg.color_rgba —— 后者可能与滑块值不一致
    rgba = cfg.color_rgba
    resolver = getattr(cfg, "resolved_color_rgba", None)
    if callable(resolver):
        rgba = resolver()
    sprite = _render_text_sprite(cfg.text, font, rgba, cfg.angle)

    # 创建透明覆盖层
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))

    if cfg.tile:
        # 平铺模式
        tile_grid(
            overlay, sprite,
            scaled_px(cfg.spacing_x, scale), scaled_px(cfg.spacing_y, scale),
            cfg.stagger,
        )
    else:
        # 单次定位（支持九宫格 / 自定义坐标）
        pos_mode = getattr(cfg, 'position_mode', 'grid')
        if pos_mode == "custom":
            cx = getattr(cfg, 'custom_x', 50)
            cy = getattr(cfg, 'custom_y', 50)
            x, y = custom_anchor_position(base.size, sprite.size, cx, cy)
        else:
            x, y = anchor_position(
                base.size, sprite.size, cfg.position, scaled_px(cfg.margin, scale)
            )
        # alpha_composite 而非 paste(..., sprite)：见 tile_grid 中的说明
        overlay.alpha_composite(sprite, (x, y))

    # Alpha 合成
    return Image.alpha_composite(base, overlay)


# ============================================================
# 图片/Logo 水印渲染
# ============================================================

def add_image_watermark(base: Image.Image, cfg, scale: float = 1.0) -> Image.Image:
    """
    为图片添加 Logo/图片水印

    :param scale: 本次渲染分辨率 / 参考分辨率，见 add_text_watermark。
                  logo 边距、平铺间距、宽度下限按它换算。
    """
    import os

    # Logo 路径有效性检查
    if not getattr(cfg, 'logo_path', None) or not os.path.exists(cfg.logo_path):
        return base

    base = base.convert("RGBA")

    # 加载并缩放 Logo
    # with 确保源文件句柄及时释放 —— Windows 下否则会导致 Logo 文件无法移动/删除
    with Image.open(cfg.logo_path) as src_logo:
        src_logo.load()
        logo = src_logo.convert("RGBA")
    # 先裁掉 Logo 自带的透明边框：这样 scale_ratio 对应"看得见的宽度"，
    # 也能避免带留白的 Logo 把平铺间距撑大
    logo = crop_to_ink(logo)
    target_w = max(max(1, scaled_px(MIN_LOGO_PX, scale)), int(base.width * cfg.scale_ratio))
    target_h = max(1, int(logo.height * target_w / logo.width))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    # 不透明度调节（仅缩放 alpha 通道，保留原透明底）
    opacity_factor = cfg.logo_opacity / 100.0
    if opacity_factor < 1.0:
        alpha = logo.getchannel("A").point(lambda a: int(a * opacity_factor))
        logo.putalpha(alpha)

    # 旋转角度
    logo_angle = getattr(cfg, 'logo_angle', 0)
    if logo_angle:
        logo = logo.rotate(-logo_angle, expand=True, resample=Image.BICUBIC)
        logo = crop_to_ink(logo)  # 旋转扩充出来的透明角同样要裁掉

    # 创建透明覆盖层
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))

    if cfg.logo_tile:
        # 平铺模式（支持交错排列 —— v5 之前该参数从未传给引擎）
        tile_grid(
            overlay, logo,
            scaled_px(cfg.logo_spacing_x, scale), scaled_px(cfg.logo_spacing_y, scale),
            getattr(cfg, 'logo_stagger', False),
        )
    else:
        # 单次定位（支持九宫格 / 自定义坐标）
        pos_mode = getattr(cfg, 'logo_position_mode', 'grid')
        if pos_mode == "custom":
            cx = getattr(cfg, 'logo_custom_x', 50)
            cy = getattr(cfg, 'logo_custom_y', 50)
            x, y = custom_anchor_position(base.size, logo.size, cx, cy)
        else:
            x, y = anchor_position(
                base.size, logo.size, cfg.logo_position,
                scaled_px(cfg.logo_margin, scale),
            )
        # alpha_composite 而非 paste(..., logo)：见 tile_grid 中的说明
        overlay.alpha_composite(logo, (x, y))

    return Image.alpha_composite(base, overlay)


# ============================================================
# 统一入口
# ============================================================

def apply_watermark(img: Image.Image, cfg, scale: float = 1.0) -> Image.Image:
    """
    统一水印应用入口。根据 cfg.watermark_type 分发到对应处理函数。

    支持的类型：
    - "text": 仅文字水印
    - "image": 仅图片水印
    - "both": 同时应用两种水印（先文字后图片）

    :param scale: 本次渲染分辨率 / 参考分辨率（导出分辨率）。
                  预览在缩小图上渲染时传入它，绝对像素类参数（间距、边距、
                  字号下限）会等比换算，于是预览与导出的几何完全一致。
                  导出请保持默认 1.0。
    """
    result = img

    # 应用文字水印
    if cfg.watermark_type in ("text", "both"):
        result = add_text_watermark(result, cfg, scale)

    # 应用图片水印
    if cfg.watermark_type in ("image", "both"):
        if cfg.logo_path and os.path.isfile(cfg.logo_path):
            result = add_image_watermark(result, cfg, scale)
        # 若 logo_path 不存在且类型为 image，静默跳过（不报错）

    return result


# ============================================================
# 智能保存 — 格式转换 + 安全 EXIF
# ============================================================

def save_with_format(
    img: Image.Image,
    path: str,
    jpeg_quality: int = 90
) -> None:
    """
    智能保存图片，根据扩展名自动选择格式和模式转换。

    功能：
    - JPEG: RGBA→RGB（白底）+ 安全 EXIF（Orientation 已清除）
    - PNG: 保持 RGBA
    - TIFF/WebP: 支持 EXIF 透传
    - 其他: 直接保存
    """
    ext = os.path.splitext(path)[1].lower()
    safe_exif = prepare_exif_for_save(img)

    if ext in (".jpg", ".jpeg"):
        # JPEG 不支持 alpha/RGBA → 转 RGB（白底）
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        kwargs = {"quality": jpeg_quality}
        if safe_exif:
            kwargs["exif"] = safe_exif

    elif ext == ".png":
        # PNG 保留 RGBA
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        kwargs = {}
        # PNG 的 EXIF 支持有限，部分查看器忽略；如需保留可取消下行注释
        # if safe_exif:
        #     kwargs["exif"] = safe_exif

    elif ext in (".tiff", ".webp"):
        # TIFF/WebP 支持 EXIF
        kwargs = {}
        if safe_exif:
            kwargs["exif"] = safe_exif

    else:
        # 其他格式（BMP 等）
        kwargs = {}

    # 确保输出目录存在
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    img.save(path, **kwargs)


# 注意：这里曾经还有一份 sanitize_path 与 _PATH_CLEAN_RE，
# 与 file_manager 的实现逐字相同、且全项目无人引用 —— 已删除。
# 路径清洗的唯一实现是 src/file_manager.sanitize_path。


# ============================================================
# 命令行测试入口
# ============================================================

if __name__ == "__main__":
    """命令行单张测试：python -m src.watermark_engine <输入图片> <输出图片>"""
    import sys

    if len(sys.argv) < 3:
        print("用法: python -m src.watermark_engine <输入图片> <输出图片>")
        print("示例: python -m src.watermark_engine photo.jpg output.jpg")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    print(f"[引擎测试] 输入: {input_path}")
    print(f"[引擎测试] 输出: {output_path}")

    # 使用默认配置
    # 不透明度只通过 opacity 指定 —— color_rgba 的 alpha 由它派生。
    # 这里若再另写一个 alpha，只会和 opacity 打架（构造时也会被覆盖掉）。
    from src.config import WatermarkConfig
    default_cfg = WatermarkConfig(
        watermark_type="text",
        text="内部资料\nConfidential",
        opacity=50,
        font_ratio=0.04,
        angle=30,
        tile=True,
        spacing_x=150,
        spacing_y=150,
        stagger=True,
    )

    # 执行
    img = safe_open_image(input_path)
    result = apply_watermark(img, default_cfg)
    save_with_format(result, output_path, jpeg_quality=90)

    print("[引擎测试] 完成!")
