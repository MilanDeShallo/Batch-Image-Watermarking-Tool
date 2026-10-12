"""
界面冒烟测试 —— 真实装配一次 AppWindow，走一遍完整操作流程

为什么需要它：
    单元测试各自都对，不代表"在窗口里点一遍"也是对的。
    作用范围三档、专属标记、翻页取值、PDF 输出与合并这些能力，
    胶水全在 AppWindow 里，只有把窗口真的建起来才能验证。

安全约定：
    1. 窗口创建后立刻 withdraw + 全透明，不打扰正在用电脑的人
    2. 所有弹窗（messagebox / 完成对话框）都被替换成记录器，不会阻塞
    3. APPDATA 指向临时目录，绝不读写用户真实的会话/窗口位置文件

用法:
    python tests/smoke_gui.py
退出码 0 表示全部通过。
"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------
# 1. 环境隔离与弹窗屏蔽（必须在 import 界面代码之前）
# ---------------------------------------------------------------

# 全部中间产物放系统临时目录，不在仓库里留垃圾
_WORK = tempfile.mkdtemp(prefix="wm_smoke_")

# app_window 在模块级用 APPDATA 拼 SESSION_FILE / 窗口位置文件
os.environ["APPDATA"] = os.path.join(_WORK, "appdata")
os.makedirs(os.environ["APPDATA"], exist_ok=True)

import tkinter.messagebox as _mb  # noqa: E402

for _fn in ("showinfo", "showwarning", "showerror"):
    setattr(_mb, _fn, lambda *a, **k: None)
_mb.askyesno = lambda *a, **k: False  # 崩溃恢复一律选"不恢复"

from PIL import Image  # noqa: E402
from reportlab.pdfgen import canvas as rl_canvas  # noqa: E402

from src.config import WatermarkConfig  # noqa: E402
from src.overrides import SCOPE_ALL, SCOPE_FILE, SCOPE_PAGE  # noqa: E402
from src.pdf_engine import (  # noqa: E402
    get_pdf_page_count,
    render_pdf_page,
)
from src.ui.app_window import AppWindow  # noqa: E402


# ---------------------------------------------------------------
# 2. 断言收集
# ---------------------------------------------------------------

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not cond:
        line += f"  <- {detail}"
    print(line, flush=True)
    return bool(cond)


def head(title):
    print(f"\n=== {title} ===", flush=True)


# ---------------------------------------------------------------
# 3. 像素工具
# ---------------------------------------------------------------

def count(img, channel):
    """通道占优像素数"""
    px = img.convert("RGB").get_flattened_data()
    if channel == "red":
        return sum(1 for q in px if q[0] > 200 and q[1] < 100 and q[2] < 100)
    if channel == "green":
        return sum(1 for q in px if q[1] > 200 and q[0] < 100 and q[2] < 100)
    return sum(1 for q in px if q[2] > 200 and q[0] < 100 and q[1] < 100)


def make_image(path, size=(800, 480), color=(0, 0, 0)):
    Image.new("RGB", size, color).save(path)


def make_pdf(path, pages=2, size=(595, 842)):
    c = rl_canvas.Canvas(path, pagesize=size)
    for _ in range(pages):
        c.setFillColorRGB(1, 1, 1)
        c.rect(0, 0, size[0], size[1], stroke=0, fill=1)
        c.showPage()
    c.save()


def pump(app, dialogs, timeout=180.0):
    """
    启动批处理并等它结束。

    必须用 mainloop 等待，不能手工 app.update() 泵事件：
    工作线程通过 self.after() 把进度送回主线程，而 tkinter 只在
    主线程处于 mainloop 里时才允许跨线程 after，
    否则工作线程会抛 "main thread is not in main loop" 并直接死掉。

    返回 True 表示在超时前恢复可用。
    """
    state = {"done": False, "timeout": False}
    deadline = time.time() + timeout

    def _poll():
        # _on_done 在主线程里把开始按钮恢复成 normal，用它作为结束信号
        try:
            idle = str(app._start_btn.cget("state")) == "normal"
        except Exception:
            idle = False

        if idle:
            state["done"] = True
            app.quit()
            return
        if time.time() > deadline:
            state["timeout"] = True
            app.quit()
            return
        app.after(60, _poll)

    app.after(60, _poll)
    app.mainloop()
    app.update()
    return state["done"]


def is_managed(w) -> bool:
    """控件是否被 pack/grid 管理。

    窗口处于 withdraw 状态时 winfo_ismapped() 恒为 0，
    判断显示/隐藏要用 winfo_manager()（pack 后为 "pack"，
    pack_forget 后为空串）。
    """
    try:
        return bool(w.winfo_manager())
    except Exception:
        return False


# ---------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------

def main():
    print(f"工作目录（系统临时目录）: {_WORK}", flush=True)

    src_dir = os.path.join(_WORK, "src")
    out_a = os.path.join(_WORK, "out_single")
    out_b = os.path.join(_WORK, "out_merged")
    for d in (src_dir, out_a, out_b):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))

    A = os.path.join(src_dir, "A.png")
    B = os.path.join(src_dir, "B.png")
    C = os.path.join(src_dir, "C.pdf")
    make_image(A)
    make_image(B)
    make_pdf(C, pages=2)

    head("创建窗口")
    app = AppWindow()
    app.withdraw()
    try:
        app.attributes("-alpha", 0.0)
    except Exception:
        pass
    app.update()

    # 弹窗一律记录，不显示
    dialogs = []
    app._show_message = lambda m: dialogs.append(("msg", m))
    app._show_completion_dialog = lambda o, s, f, note=None: dialogs.append(
        ("done", o, s, f, note)
    )

    # 关掉预览的后台渲染线程：它在工作线程里调 self.after()，
    # 而本次冒烟在批处理之外是同步驱动（不在 mainloop 里），
    # 跨线程 after 会抛 "main thread is not in main loop"。
    # 预览像素不是本次验证对象，直接不渲染，顺带排除线程干扰。
    app._preview_canvas._render_in_background = lambda *a, **k: None

    check("窗口已建成且隐藏", app.winfo_exists() == 1)

    # ---------- 加文件 ----------
    head("添加文件")
    app._file_list.add_files([A, B, C])
    app._update_count()
    app.update()
    check("文件列表 3 项", app._file_list.count == 3, app._file_list.count)

    # ---------- 列表选中与拖拽排序 ----------
    head("文件列表：选中与拖拽排序")
    # 点行会走 select_by_index。历史上它末尾调了 CTkFrame 没有的 see()，
    # 异常把 on_press 里后面的拖拽状态赋值一并跳过 —— 表现为"能选中但拖不动"，
    # 而且异常只进 stderr，打包成 exe 后完全看不见
    sel_err = ""
    try:
        app._file_list.select_by_index(0)
    except Exception as e:
        sel_err = repr(e)
    check("点击选中不抛异常", not sel_err, sel_err)

    before = list(app._file_list.files)
    app._file_list._reorder(0, 2)
    mid = list(app._file_list.files)
    check("拖拽重排改变了顺序",
          sorted(mid) == sorted(before) and mid != before, mid)
    app._file_list._reorder(2, 0)
    check("顺序可还原", list(app._file_list.files) == before, app._file_list.files)
    app.update()

    # ---------- 设全局默认 ----------
    head("作用范围：全部文件")
    base_cfg = WatermarkConfig(
        watermark_type="text", text="全局", font_ratio=0.12,
        color_rgba=(255, 0, 0, 255), opacity=100,
        position="bottom-right", margin=20,
    )
    app._on_scope_changed(SCOPE_ALL)
    app._text_panel.set_from_config(base_cfg)
    app.update()
    app._commit_current_layer()
    app.update()

    check("当前档位=全部文件", app._scope == SCOPE_ALL, app._scope)
    check("全局默认已记录文字", app._scopes.config_for(None).text == "全局",
          app._scopes.config_for(None).text)
    check("全局默认字号正确",
          abs(app._scopes.config_for(None).font_ratio - 0.12) < 1e-6,
          app._scopes.config_for(None).font_ratio)

    # ---------- B 文件设专属 ----------
    head("作用范围：仅本文件（B）")
    app._file_list.select_by_index(1)
    app.update()
    check("选中项为 B", app._file_list.selected_file == B, app._file_list.selected_file)
    check("B 无专属时档位未自动改变", app._scope == SCOPE_ALL, app._scope)

    app._on_scope_changed(SCOPE_FILE)
    b_cfg = WatermarkConfig(
        watermark_type="text", text="仅B", font_ratio=0.12,
        color_rgba=(0, 0, 255, 255), opacity=100,
        position="bottom-right", margin=20,
    )
    app._text_panel.set_from_config(b_cfg)
    app.update()
    app._commit_current_layer()
    app.update()

    check("B 有文件层专属", app._scopes.has_file(B))
    patch = app._scopes.to_dict()["files"].get(B, {})
    check("文件层只存差异字段（非整份快照）",
          set(patch) <= {"text", "color_rgba"},
          sorted(patch))
    check("B 生效文字=仅B", app._scopes.config_for(B).text == "仅B",
          app._scopes.config_for(B).text)
    marker = app._file_list._marker_labels[1].cget("text")
    check("B 行显示专属标记", marker == "专属", marker)

    # ---------- A 不受影响 ----------
    app._file_list.select_by_index(0)
    app.update()
    check("A 仍走全局默认", app._scopes.config_for(A).text == "全局",
          app._scopes.config_for(A).text)
    check("A 行无专属标记",
          app._file_list._marker_labels[0].cget("text") == "",
          app._file_list._marker_labels[0].cget("text"))

    # ---------- C 的页层专属 ----------
    head("作用范围：仅本页（C.pdf）")
    app._file_list.select_by_index(2)
    app.update()
    check("选中项为 C", app._file_list.selected_file == C, app._file_list.selected_file)
    check("C 预览当前页=0", app._preview_canvas.current_pdf_page == 0)

    # 第 1 页：不加（文字置空）
    app._on_scope_changed(SCOPE_PAGE)
    blank = dict(base_cfg.to_dict())
    blank["text"] = ""
    app._text_panel.set_from_config(WatermarkConfig.from_dict(blank))
    app.update()
    app._commit_current_layer()
    app.update()
    check("C 第 1 页有页层专属", app._scopes.has_page(C, 0))
    check("第 1 页专属只有 text 差异",
          set(app._scopes.to_dict()["pages"][0]["data"]) == {"text"},
          app._scopes.to_dict()["pages"][0]["data"])

    # 翻到第 2 页：面板应回填「全局默认」（C 无文件层）
    app._preview_canvas._set_pdf_page(1)
    app.update()
    check("翻页后当前页=1", app._preview_canvas.current_pdf_page == 1)
    panel_text = app._text_panel.get_config_dict().get("text")
    check("翻页后面板回填的是该页生效参数", panel_text == "全局", repr(panel_text))

    c2_cfg = WatermarkConfig(
        watermark_type="text", text="仅C2", font_ratio=0.12,
        color_rgba=(0, 255, 0, 255), opacity=100,
        position="bottom-right", margin=20,
    )
    app._text_panel.set_from_config(c2_cfg)
    app.update()
    app._commit_current_layer()
    app.update()

    check("C 第 2 页有页层专属", app._scopes.has_page(C, 1))
    check("C 两页都有专属", app._scopes.page_numbers(C) == [0, 1],
          app._scopes.page_numbers(C))
    marker = app._file_list._marker_labels[2].cget("text")
    check("C 行标记显示页数", marker == "专属 2页", marker)
    marked = app._preview_canvas._pdf_marked_label.cget("text")
    check("翻页栏显示专属页", "专属页" in marked and "1" in marked and "2" in marked,
          marked)

    # ---------- 自动定位到最深一层 ----------
    head("自动定位作用层")
    app._scope = SCOPE_ALL
    app._on_file_selected(B)
    app.update()
    check("选中带文件层专属的 B → 自动切到「仅本文件」",
          app._scope == SCOPE_FILE, app._scope)

    app._scope = SCOPE_ALL
    app._on_file_selected(C)
    app.update()
    check("选中带页层专属的 C → 自动切到「仅本页」",
          app._scope == SCOPE_PAGE, app._scope)

    # ---------- 清除专属 ----------
    head("清除专属参数")
    app._file_list.select_by_index(1)  # 必须先选中 B：清除作用于"当前选中"
    app.update()
    check("清除前选中项为 B", app._file_list.selected_file == B,
          app._file_list.selected_file)
    app._on_scope_changed(SCOPE_FILE)
    app._on_clear_scope_override()
    app.update()
    check("清除 B 的文件层", not app._scopes.has_file(B))
    check("B 行标记已消失",
          app._file_list._marker_labels[1].cget("text") == "",
          app._file_list._marker_labels[1].cget("text"))
    # 还原 B 的专属，供后面导出验证
    app._scopes.set_file(B, {"text": "仅B", "color_rgba": [0, 0, 255, 255]})
    app._refresh_scope_marks()
    app.update()

    # ---------- 格式切换 ----------
    head("输出格式：PDF")
    app._format_var.set("PDF")
    app._on_format_changed("PDF")
    app.update()
    check("选 PDF 后出现合并开关", is_managed(app._merge_pdf_cb))
    app._format_var.set("PNG")
    app._on_format_changed("PNG")
    app.update()
    check("切回 PNG 后合并开关隐藏且复位",
          not is_managed(app._merge_pdf_cb) and not app._merge_pdf_var.get())

    # ---------- 导出：PDF + 合并 ----------
    head("导出：PDF（合并为单个）")
    app._format_var.set("PDF")
    app._on_format_changed("PDF")
    app._merge_pdf_var.set(True)
    app._output_dir_var.set(out_b)
    app.update()

    app._on_start_processing()
    ok = pump(app, dialogs)
    check("批处理在超时前结束", ok)
    app.update()

    merged = [f for f in os.listdir(out_b) if f.lower().endswith(".pdf")]
    check("合并输出恰好 1 个 PDF", len(merged) == 1, merged)
    if merged:
        mp = os.path.join(out_b, merged[0])
        check("输出名带「合并水印」前缀", merged[0].startswith("合并水印"), merged[0])
        pages = get_pdf_page_count(mp)
        check("总页数 = 1(A) + 1(B) + 2(C) = 4", pages == 4, pages)
        if pages == 4:
            p0 = render_pdf_page(mp, 0, scale=1.0)
            p1 = render_pdf_page(mp, 1, scale=1.0)
            p2 = render_pdf_page(mp, 2, scale=1.0)
            p3 = render_pdf_page(mp, 3, scale=1.0)
            check("第1页(A)=红色全局水印", count(p0, "red") > 100 and count(p0, "blue") == 0,
                  (count(p0, "red"), count(p0, "blue")))
            check("第2页(B)=蓝色文件层水印", count(p1, "blue") > 100 and count(p1, "red") == 0,
                  (count(p1, "blue"), count(p1, "red")))
            check("第3页(C 第1页)=无彩色水印",
                  count(p2, "red") == 0 and count(p2, "green") == 0 and count(p2, "blue") == 0,
                  (count(p2, "red"), count(p2, "green"), count(p2, "blue")))
            check("第4页(C 第2页)=绿色页层水印", count(p3, "green") > 50,
                  count(p3, "green"))
    check("完成对话框已弹出（合并文案带页数）",
          any(d[0] == "done" and d[4] and "页" in str(d[4]) for d in dialogs),
          dialogs[-1:] if dialogs else [])

    # ---------- 导出：PDF 不合并 ----------
    head("导出：PDF（每图一个单页 PDF）")
    dialogs.clear()
    app._merge_pdf_var.set(False)
    app._output_dir_var.set(out_a)
    app.update()

    app._on_start_processing()
    ok = pump(app, dialogs)
    app.update()
    check("批处理在超时前结束", ok)

    made = sorted(f for f in os.listdir(out_a) if f.lower().endswith(".pdf"))
    check("逐文件输出 3 个 PDF", len(made) == 3, made)
    counts = {}
    for f in made:
        counts[f] = get_pdf_page_count(os.path.join(out_a, f))
    check("A/B 各 1 页、C 为 2 页",
          sorted(counts.values()) == [1, 1, 2], counts)
    a_pdf = os.path.join(out_a, "A_watermarked.pdf")
    b_pdf = os.path.join(out_a, "B_watermarked.pdf")
    if os.path.exists(a_pdf) and os.path.exists(b_pdf):
        check("A 输出红色水印", count(render_pdf_page(a_pdf, 0, scale=1.0), "red") > 100)
        check("B 输出蓝色水印", count(render_pdf_page(b_pdf, 0, scale=1.0), "blue") > 100)
    else:
        check("输出文件名符合模板", False, made)

    # ---------- 会话往返 ----------
    head("会话持久化")
    app._save_session()
    session_file = os.path.join(os.environ["APPDATA"], "WatermarkTool", "session.json")
    check("会话文件已写出", os.path.exists(session_file), session_file)
    if os.path.exists(session_file):
        import json
        with open(session_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        check("会话包含 scopes", isinstance(data.get("scopes"), dict))
        check("会话包含 merge_pdf 开关", "merge_pdf" in data)
        check("会话里的文件层保留了 B",
              B in (data.get("scopes", {}).get("files") or {}),
              list((data.get("scopes", {}).get("files") or {}).keys()))

    # ---------- 收尾 ----------
    head("收尾")
    app._clear_session()
    try:
        app.destroy()
    except Exception:
        pass
    check("窗口已销毁", True)

    # ---------- 汇总 ----------
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 56, flush=True)
    print(f"冒烟结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过", flush=True)
    if failed:
        print("失败项:", flush=True)
        for name, _, detail in failed:
            print(f"  - {name}  {detail}", flush=True)
    print("=" * 56, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
