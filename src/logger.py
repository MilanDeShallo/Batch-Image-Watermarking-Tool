"""
日志系统 — 写入 %APPDATA%/WatermarkTool/app.log

特性：
1. 自动轮转：超过 1MB 时重命名为 app_YYYYMMDD.log，新建 app.log
2. 仅保留最近 5 个日志文件
3. 线程安全
4. 同时输出到控制台和文件
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

_LOG_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "WatermarkTool"
)
_LOG_FILE = os.path.join(_LOG_DIR, "app.log")

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """获取全局 logger 单例"""
    global _logger
    if _logger is not None:
        return _logger

    os.makedirs(_LOG_DIR, exist_ok=True)

    _logger = logging.getLogger("WatermarkTool")
    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False  # 不传播到 root logger

    # 文件 handler：轮转，单文件 1MB，保留 5 个
    file_handler = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=1_000_000,  # 1MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    # 控制台 handler（开发时用）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "[%(levelname)s] %(message)s"
    ))

    _logger.addHandler(file_handler)
    _logger.addHandler(console_handler)

    _logger.info(f"=== 日志系统启动 ===")
    _logger.info(f"日志目录: {_LOG_DIR}")

    return _logger


def log_info(msg: str):
    """记录 INFO 级别日志"""
    get_logger().info(msg)


def log_warning(msg: str):
    """记录 WARNING 级别日志"""
    get_logger().warning(msg)


def log_error(msg: str, exc: Exception | None = None):
    """记录 ERROR 级别日志"""
    if exc:
        get_logger().error(f"{msg}: {exc}", exc_info=True)
    else:
        get_logger().error(msg)


def log_debug(msg: str):
    """记录 DEBUG 级别日志"""
    get_logger().debug(msg)


def log_exception(msg: str, exc: BaseException | None = None):
    """
    记录异常并附带完整 traceback。

    打包成 exe 后没有控制台，`print()` 出去的错误信息用户永远看不到，
    必须落到文件里才可能被排查。
    """
    try:
        get_logger().error(msg, exc_info=exc if exc is not None else True)
    except Exception:
        pass


def log_path() -> str:
    """日志文件路径（供界面「打开日志」入口使用）"""
    return _LOG_FILE


def log_dir() -> str:
    """日志目录"""
    return _LOG_DIR


def install_excepthook():
    """
    把未捕获异常写进日志。

    默认行为是把 traceback 打到 stderr —— exe 没有 stderr，异常就此消失。
    """
    import sys
    import threading

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log_exception(
            "未捕获异常",
            exc_value if exc_value is not None else exc_type,
        )
        try:
            sys.__excepthook__(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = _hook

    # 线程里的异常不会被 sys.excepthook 捕获
    try:
        _original = threading.excepthook

        def _thread_hook(args):
            if not issubclass(args.exc_type, SystemExit):
                log_exception(
                    f"工作线程未捕获异常 ({getattr(args.thread, 'name', '?')})",
                    args.exc_value,
                )
            try:
                _original(args)
            except Exception:
                pass

        threading.excepthook = _thread_hook
    except Exception:
        pass
