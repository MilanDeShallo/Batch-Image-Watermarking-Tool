"""
批量图片水印工具 - 入口文件

启动顺序有讲究：**先装日志与全局异常钩子，再建窗口**。
打包成 exe 后没有控制台，任何在界面起来之前抛出的异常若没人接住，
用户只会看到窗口闪一下就没了，连去哪找原因都不知道。
"""

import os
import sys

# 确保项目根目录在 sys.path 中（支持直接运行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    from src import __version__
    from src.logger import get_logger, install_excepthook, log_exception, log_path

    logger = get_logger()
    install_excepthook()
    # 版本写进日志：用户报问题时第一件事就是确认他跑的是哪一版
    logger.info(f"=== 应用启动 v{__version__} ===")

    try:
        from src.ui.app_window import AppWindow

        app = AppWindow()

        # tkinter 会吞掉回调里的异常（默认只往 stderr 打一行），
        # exe 没有 stderr，等同于静默失败，必须自己接管
        def _tk_report(exc_type, exc_value, exc_tb):
            logger.error(
                "界面回调异常",
                exc_info=(exc_type, exc_value, exc_tb),
            )

        app.report_callback_exception = _tk_report
        app.mainloop()
        return 0

    except Exception as exc:
        log_exception("应用异常退出", exc)
        try:
            from tkinter import messagebox
            messagebox.showerror(
                "启动失败",
                f"程序启动时出错。\n\n日志：{log_path()}\n\n{exc}",
            )
        except Exception:
            pass
        return 1

    finally:
        logger.info("=== 应用退出 ===")


if __name__ == "__main__":
    sys.exit(main())
