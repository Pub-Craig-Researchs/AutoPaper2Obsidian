import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(r"<YOUR_PROJECT_ROOT>")

# 输入/输出路径
PAPERS_RAW = PROJECT_ROOT / "papers_raw"
PAPERS_LEGACY = PROJECT_ROOT / "papers_legacy"

# Obsidian 配置
OBSIDIAN_VAULT = Path(r"<YOUR_OBSIDIAN_VAULT_PATH>")
OBSIDIAN_ATTACHMENTS = OBSIDIAN_VAULT / "attachments"

# MinerU 配置
MINERU_API = "http://localhost:8866"
MINERU_START_SCRIPT = r"<YOUR_MINERU_START_SCRIPT_PATH>"
MINERU_STOP_SCRIPT = r"<YOUR_MINERU_STOP_SCRIPT_PATH>"

# LLM 配置
API_CONFIG_PATH = PROJECT_ROOT / "api_config.json"

# 日志
LOG_DIR = PROJECT_ROOT / "logs"

# 处理映射日志
MAPPING_LOG = LOG_DIR / "mapping_log.jsonl"

# 临时处理目录（MinerU 输出）
TEMP_OUTPUT_DIR = PROJECT_ROOT / "temp_output"
