"""Logging with rotation; secrets are never logged by callers."""

# 中文说明：
# 日志系统解决两个问题：
#   1. 程序在后台跑，出了问题要能事后查 —— 写文件；
#   2. 文件不能无限长大 —— RotatingFileHandler 自动“切蛋糕”。
#
# 轮转（rotation）的意思是：bot.log 写到 max_bytes 就改名为 bot.log.1，
# 新的 bot.log 继续写；最多保留 backups 个旧文件，更老的自动删除。
# 这样磁盘占用有上限，又保留最近的现场。

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 模块级开关：setup_logging 可能被调用多次（例如测试或重复初始化），
# 用这个标记避免同一个进程里重复挂 handler（否则每条日志会打印多遍）。
_CONFIGURED = False


def setup_logging(
    log_dir: str | Path,
    *,
    level: str = "INFO",
    max_bytes: int = 5_000_000,
    backups: int = 5,
    console: bool = True,
    filename: str = "bot.log",
) -> logging.Logger:
    global _CONFIGURED
    # 整个项目统一使用名为 "bot" 的父 logger。
    # 子模块通过 get_logger("xxx") 拿到 "bot.xxx"，天然形成层级。
    logger = logging.getLogger("bot")
    if _CONFIGURED:
        # 已经配置过：只更新级别，不再重复添加 handler。
        logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        return logger
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # propagate=False 防止日志同时被 Python 根 logger 再输出一遍。
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    directory = Path(log_dir)
    # parents=True：父目录不存在也一起创建；exist_ok=True：已存在不报错。
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        directory / filename,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",  # 中文日志必须显式 UTF-8，Windows 默认编码可能是 GBK
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console:
        # 控制台输出走 stderr，不污染 stdout 里的 JSON/命令结果。
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    _CONFIGURED = True
    return logger


def get_logger(name: str) -> logging.Logger:
    # 例如 get_logger(__name__) 在 bot.worker 中得到 "bot.bot.worker"，
    # 虽然名字有点长，但仍然挂在 "bot" 层级下，会继承文件/控制台 handler。
    return logging.getLogger(f"bot.{name}")
