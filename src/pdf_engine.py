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

def _make_overlay_pdf(page_w: float, page_h: float, cfg) -> bytes:
    """
    生成与页面同尺寸的透明覆盖 PDF（单页，含水印 PNG）。

    :param page_w: 页面宽（PDF 点，1/72 英寸）
    :param page_h: 页面高（PDF 点）
    :param cfg: WatermarkConfig
    :return: 覆盖 PDF 的字节内容
    """
    from src.watermark_engine import apply_watermark

    # 1. 全透明底图（页面尺寸 × 渲染倍率）
    img_w = max(1, int(page_w * OVERLAY_SCALE))
    img_h = max(1, int(page_h * OVERLAY_SCALE))
    base = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))

    # 2. 现有引擎渲染水印（相对几何与预览一致）
    overlay_img = apply_watermark(base, cfg)

    # 3. 导出为 PNG（保留 alpha）
    png_buf = io.BytesIO()
    overlay_img.save(png_buf, format="PNG")
    png_buf.seek(0)

    # 4. reportlab 包装为页面尺寸的 PDF
    pdf_buf = io.BytesIO()
    c = canvas.Canvas(pdf_buf, pagesize=(page_w, page_h))
    c.drawImage(
        ImageReader(png_buf),
        0, 0,
        width=page_w, height=page_h,
        mask='auto',  # 保留 PNG 透明通道
    )
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

    # 按页面尺寸缓存覆盖层（多页同尺寸只生成一次）
    overlay_cache: dict[tuple, bytes] = {}

    for page in writer.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        key = (round(w, 1), round(h, 1))
        if key not in overlay_cache:
            overlay_cache[key] = _make_overlay_pdf(w, h, cfg)

        overlay_reader = PdfReader(io.BytesIO(overlay_cache[key]))
        # merge_page：覆盖层内容叠加在原内容之上
        page.merge_page(overlay_reader.pages[0])

    with open(output_path, "wb") as f:
        writer.write(f)

    return len(writer.pages)
