"""公共工具函数"""
import logging
import os
import re
import time
from pathlib import Path
from functools import wraps
from typing import Optional

from rich.logging import RichHandler

from src.config import (
    LOG_DIR,
    PAPERS_LEGACY,
    OBSIDIAN_VAULT,
    OBSIDIAN_ATTACHMENTS,
    TEMP_OUTPUT_DIR,
)


def setup_logging(name: str) -> logging.Logger:
    """日志初始化，同时输出到控制台（Rich handler）和文件
    
    Args:
        name: 日志器名称
        
    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 控制台 Rich handler
    rich_handler = RichHandler(rich_tracebacks=True)
    rich_handler.setLevel(logging.INFO)
    console_format = logging.Formatter("%(message)s")
    rich_handler.setFormatter(console_format)
    logger.addHandler(rich_handler)
    
    # 文件 handler
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "autopaper.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
    
    return logger


def sanitize_filename(name: str) -> str:
    """文件名消毒（移除 Windows 非法字符，截断到 200 字符）
    
    Args:
        name: 原始文件名
        
    Returns:
        消毒后的文件名
    """
    # Windows 非法字符: <>:"/\|?*
    illegal_chars = r'[<>:"/\\|?*]'
    sanitized = re.sub(illegal_chars, "", name)
    
    # 截断到 200 字符
    if len(sanitized) > 200:
        sanitized = sanitized[:200]
    
    # 去除首尾空白
    sanitized = sanitized.strip()
    
    return sanitized


def safe_move(src: Path, dst: Path) -> Path:
    """安全文件移动（目标已存在则加后缀 _1, _2...）
    
    Args:
        src: 源文件路径
        dst: 目标文件路径
        
    Returns:
        最终的目标文件路径
    """
    if not dst.exists():
        os.makedirs(dst.parent, exist_ok=True)
        src.rename(dst)
        return dst
    
    # 目标已存在，添加后缀
    stem = dst.stem
    suffix = dst.suffix
    parent = dst.parent
    
    counter = 1
    while True:
        new_dst = parent / f"{stem}_{counter}{suffix}"
        if not new_dst.exists():
            os.makedirs(new_dst.parent, exist_ok=True)
            src.rename(new_dst)
            return new_dst
        counter += 1


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0):
    """指数退避重试装饰器
    
    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟时间（秒）
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries:
                        raise
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
            return None
        return wrapper
    return decorator


def ensure_dirs():
    """确保所有必要的目录存在"""
    dirs_to_create = [
        LOG_DIR,
        PAPERS_LEGACY,
        OBSIDIAN_VAULT,
        OBSIDIAN_ATTACHMENTS,
        TEMP_OUTPUT_DIR,
    ]
    
    for dir_path in dirs_to_create:
        dir_path.mkdir(parents=True, exist_ok=True)
