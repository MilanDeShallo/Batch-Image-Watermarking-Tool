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
