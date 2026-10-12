"""
PDF 水印引擎 — 复用 Pillow 水印引擎渲染覆盖层

设计：
1. 用现有 apply_watermark() 在全透明底图上渲染水印 → 得到透明 PNG 覆盖层
   （平铺、旋转、透明度、自定义坐标等所有现有设置对 PDF 全部生效）
2. reportlab 将 PNG 覆盖层包装为与目标页面同尺寸的覆盖 PDF 页
3. pypdf 将覆盖层逐页合并到原 PDF（原内容保留，文字仍可选可搜）

依赖许可证（与项目 MIT 兼容）：
- reportlab (BSD)
- pypdf (BSD)
- pypdfium2 (Apache-2.0/BSD) — 仅用于预览渲染

预览一致性：
- 预览：pypdfium2 渲染页面 → Pillow 引擎叠加水印
- 导出：透明底图 → 同一 Pillow 引擎 → 覆盖层合并
- 两者使用相同的相对几何计算，所见即所得
"""

from __future__ import annotations

import io
import os

from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter

# 覆盖层渲染倍率（相对 PDF 页面点数）。
# 2.0 ≈ 144 DPI，保证文字在 200% 缩放下依然清晰
OVERLAY_SCALE = 2.0


# ============================================================
# 页面渲染（预览/缩略图用）
# ============================================================

def render_pdf_page(pdf_path: str, page_num: int = 0, scale: float = OVERLAY_SCALE) -> Image.Image:
    """
    渲染 PDF 指定页为 PIL 图像（RGB）。

    :param pdf_path: PDF 文件路径
    :param page_num: 页码（0 起）
    :param scale: 渲染倍率（1.0 = 72 DPI）
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_path)
    try:
        if page_num >= len(doc):
            page_num = 0
        page = doc[page_num]
        bitmap = page.render(scale=scale)
        return bitmap.to_pil().convert("RGB")
    finally:
        doc.close()


def get_pdf_page_count(pdf_path: str) -> int:
    """获取 PDF 页数"""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


# ============================================================
# 覆盖层生成
# ============================================================

def _page_geometry(page) -> tuple[float, float, int, float, float]:
    """
    解析页面几何。

    :return: (media_w, media_h, rotate, origin_x, origin_y)
    - media_w / media_h: 未旋转的 MediaBox 尺寸（PDF 点，1/72 英寸）
    - rotate: 显示时的顺时针旋转角，仅取 0 / 90 / 180 / 270
    - origin_x / origin_y: MediaBox 左下角坐标（通常为 0, 0）
    """
    mb = page.mediabox
    media_w = abs(float(mb.width))
    media_h = abs(float(mb.height))
    origin_x = float(mb.left)
    origin_y = float(mb.bottom)

    try:
        rotate = int(page.rotation or 0) % 360
    except Exception:
        rotate = 0
    if rotate not in (0, 90, 180, 270):
        rotate = 0  # 非法角度按不旋转处理

    return media_w, media_h, rotate, origin_x, origin_y


def visual_size(media_w: float, media_h: float, rotate: int) -> tuple[float, float]:
    """页面在屏幕上显示时的尺寸（90 / 270 度旋转会交换宽高）"""
    if rotate in (90, 270):
        return media_h, media_w
    return media_w, media_h


def _overlay_placement(media_w: float, media_h: float, rotate: int) -> tuple[float, float, int]:
    """
    覆盖层内容在页面坐标系中的摆放参数：(平移 x, 平移 y, 逆时针旋转角)。

    为什么需要反向旋转：
    - /Rotate 的含义是「显示前把整页顺时针旋转 R 度」，覆盖层与原内容
      共用同一份未旋转的页面坐标系，所以水印图必须反向旋转 R 度，
      显示出来才是正的；
    - 平移量保证图像旋转后正好覆盖整页：
        0°   → translate(0, 0)     rotate(0)
        90°  → translate(w, 0)     rotate(90)
        180° → translate(w, h)     rotate(180)
        270° → translate(0, h)     rotate(-90)
    - 旧实现直接按 MediaBox 创建覆盖层，旋转页下水印被拉伸
      （90° 页面实测横向 ×0.707、纵向 ×1.415，变形约 2 倍）。
    """
    if rotate == 90:
        return media_w, 0.0, 90
    if rotate == 180:
        return media_w, media_h, 180
    if rotate == 270:
        return 0.0, media_h, -90
    return 0.0, 0.0, 0


def _make_overlay_pdf(
    media_w: float,
    media_h: float,
    rotate: int,
    cfg,
    origin: tuple[float, float] = (0.0, 0.0),
) -> bytes:
    """
    生成与页面同尺寸的透明覆盖 PDF（单页，含水印 PNG）。

    :param media_w: 页面未旋转宽（PDF 点，1/72 英寸）
    :param media_h: 页面未旋转高（PDF 点）
    :param rotate: 页面显示旋转角（0/90/180/270）
    :param cfg: WatermarkConfig
    :param origin: MediaBox 左下角坐标
    :return: 覆盖 PDF 的字节内容
    """
    from src.watermark_engine import apply_watermark

    # 水印按「显示方向」渲染：旋转页的可视尺寸与 MediaBox 正好相反
    vis_w, vis_h = visual_size(media_w, media_h, rotate)

    # 1. 全透明底图（可视尺寸 × 渲染倍率）
    img_w = max(1, int(vis_w * OVERLAY_SCALE))
    img_h = max(1, int(vis_h * OVERLAY_SCALE))
    base = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))

    # 2. 现有引擎渲染水印（相对几何与预览一致）
    overlay_img = apply_watermark(base, cfg)

    # 3. 导出为 PNG（保留 alpha）
    png_buf = io.BytesIO()
    overlay_img.save(png_buf, format="PNG")
    png_buf.seek(0)

    # 4. reportlab 包装：页面仍是 MediaBox 尺寸，内容按显示方向反向旋转
    dx, dy, ccw = _overlay_placement(media_w, media_h, rotate)
    pdf_buf = io.BytesIO()
    c = canvas.Canvas(pdf_buf, pagesize=(media_w, media_h))
    c.saveState()
    c.translate(origin[0] + dx, origin[1] + dy)
    if ccw:
        c.rotate(ccw)
    c.drawImage(
        ImageReader(png_buf),
        0, 0,
        width=vis_w, height=vis_h,
        mask='auto',  # 保留 PNG 透明通道
    )
    c.restoreState()
    c.showPage()
    c.save()
    pdf_buf.seek(0)
    return pdf_buf.read()


# ============================================================
# 主入口
# ============================================================

def apply_pdf_watermark(pdf_path: str, cfg, output_path: str) -> int:
    """
    给 PDF 每一页加水印，输出到 output_path。

    :param pdf_path: 源 PDF 路径
    :param cfg: WatermarkConfig
    :param output_path: 输出 PDF 路径
    :return: 处理的页数
    :raises ValueError: PDF 加密或损坏
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"文件不存在: {pdf_path}")

    try:
        reader = PdfReader(pdf_path)
    except Exception as e:
        raise ValueError(f"PDF 无法打开或已损坏: {os.path.basename(pdf_path)} - {e}")

    # 加密 PDF：尝试空密码解锁，失败则报错
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError("PDF 已加密，无法添加水印")
        except ValueError:
            raise
        except Exception:
            raise ValueError("PDF 已加密，无法添加水印")

    if len(reader.pages) == 0:
        raise ValueError("PDF 没有任何页面")

    # 先复制到 writer 再合并（pypdf 推荐模式，避免 reader 页直接修改）
    writer = PdfWriter()
    writer.append(reader)

    # 按「尺寸 + 旋转角」缓存覆盖层（多页同规格只生成一次）
    overlay_cache: dict[tuple, bytes] = {}

    for page in writer.pages:
        media_w, media_h, rotate, origin_x, origin_y = _page_geometry(page)
        key = (round(media_w, 1), round(media_h, 1), rotate)
        if key not in overlay_cache:
            overlay_cache[key] = _make_overlay_pdf(
                media_w, media_h, rotate, cfg, (origin_x, origin_y)
            )

        overlay_reader = PdfReader(io.BytesIO(overlay_cache[key]))
        # merge_page：覆盖层内容叠加在原内容之上。
        # 这里不改目标页的 /Rotate —— 覆盖层已按同一角度反向旋转，
        # 两段内容仍在同一个未旋转坐标系里，显示方向自然一致。
        page.merge_page(overlay_reader.pages[0])

    with open(output_path, "wb") as f:
        writer.write(f)

    return len(writer.pages)
