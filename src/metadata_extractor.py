"""元数据提取模块 - 从 Markdown 头部提取论文元数据"""
import re
import json
from datetime import datetime
from pathlib import Path
from jsonschema import validate, ValidationError

from src.llm_client import LLMClient
from src.config import PROJECT_ROOT
from src.utils import setup_logging, sanitize_filename

logger = setup_logging("metadata_extractor")

# 元数据 JSON Schema
METADATA_SCHEMA = {
    "type": "object",
    "required": ["title", "first_author", "year"],
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "first_author": {"type": "string", "minLength": 1},
        "year": {"type": ["string", "integer"]},
        "journal": {"type": "string"},
        "doi": {"type": "string"},
        "is_appendix": {"type": "boolean"},
        "main_paper": {"type": "string", "description": "如果是附录，关联的主论文标题"}
    }
}


class MetadataExtractor:
    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self._load_prompt_template()
    
    def _load_prompt_template(self):
        """从 prompts/metadata_extract.txt 加载 Prompt 模板"""
        prompt_path = PROJECT_ROOT / "prompts" / "metadata_extract.txt"
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                self.prompt_template = f.read()
            logger.debug(f"Prompt 模板加载成功: {prompt_path}")
        except FileNotFoundError:
            logger.error(f"Prompt 模板文件未找到: {prompt_path}")
            raise
        except Exception as e:
            logger.error(f"加载 Prompt 模板失败: {e}")
            raise
    
    def extract_metadata(self, md_content: str) -> dict:
        """从 Markdown 内容提取元数据
        
        1. 取前 3000 字符作为上下文
        2. 调用 LLM 提取 JSON 格式元数据
        3. 用 jsonschema 校验
        4. 校验失败标记 status: manual_review
        
        Args:
            md_content: Markdown 格式的论文内容
            
        Returns:
            dict: 包含 title, first_author, year, journal, doi, is_appendix, main_paper, status
        """
        # 取前 3000 字符作为上下文
        context = md_content[:3000]
        
        # 构建用户提示词
        user_prompt = f"Extract metadata from the following academic paper text:\n\n{context}"
        
        try:
            # 调用 LLM 提取 JSON 格式元数据
            raw_metadata = self.llm.chat_completion_json(
                system_prompt=self.prompt_template,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=1000
            )
            
            # 用 jsonschema 校验
            try:
                validate(instance=raw_metadata, schema=METADATA_SCHEMA)
                metadata = raw_metadata
                metadata["status"] = "auto"
                logger.info(f"元数据提取成功: {metadata.get('title', 'N/A')}")
            except ValidationError as e:
                logger.warning(f"元数据校验失败: {e.message}")
                metadata = raw_metadata
                metadata["status"] = "manual_review"
                
        except Exception as e:
            logger.error(f"元数据提取失败: {e}")
            # 返回一个带有错误状态的元数据
            metadata = {
                "title": "",
                "first_author": "",
                "year": "",
                "journal": "",
                "doi": "",
                "is_appendix": False,
                "main_paper": "",
                "status": "manual_review"
            }
        
        # 确保所有必需字段存在
        metadata.setdefault("journal", "")
        metadata.setdefault("doi", "")
        metadata.setdefault("is_appendix", False)
        metadata.setdefault("main_paper", "")
        
        return metadata
    
    def generate_standard_filename(self, metadata: dict) -> str:
        """生成标准文件名: {first_author}_{year}_{short_title}
        
        - short_title: 取 title 前 5 个单词，用下划线连接
        - 通过 sanitize_filename 清理
        - 附录文件加 _appendix 后缀
        
        Args:
            metadata: 包含 title, first_author, year, is_appendix 的字典
            
        Returns:
            str: 标准文件名（不含扩展名）
        """
        first_author = metadata.get("first_author", "unknown")
        year = str(metadata.get("year", "unknown"))
        title = metadata.get("title", "")
        is_appendix = metadata.get("is_appendix", False)
        
        # 取 title 前 5 个单词
        words = title.split()[:10]
        short_title = "_".join(words)
        
        # 构建基础文件名
        filename = f"{first_author}_{year}_{short_title}"
        
        # 附录文件加后缀
        if is_appendix:
            filename += "_appendix"
        
        # 清理文件名
        filename = sanitize_filename(filename)
        
        return filename
    
    def generate_frontmatter(self, metadata: dict) -> str:
        """生成 YAML Frontmatter 字符串
        
        格式：
        ---
        title: "..."
        first_author: "..."
        year: ...
        journal: "..."
        doi: "..."
        is_appendix: true/false
        main_paper: "[[...]]"  # 仅附录有此字段
        status: "auto"  # 或 "manual_review"
        processed_date: "2026-04-20"
        ---
        
        Args:
            metadata: 元数据字典
            
        Returns:
            str: YAML Frontmatter 字符串
        """
        title = metadata.get("title", "")
        first_author = metadata.get("first_author", "")
        year = metadata.get("year", "")
        journal = metadata.get("journal", "")
        doi = metadata.get("doi", "")
        is_appendix = metadata.get("is_appendix", False)
        main_paper = metadata.get("main_paper", "")
        status = metadata.get("status", "manual_review")
        processed_date = datetime.now().strftime("%Y-%m-%d")
        
        # 构建 frontmatter
        lines = ["---"]
        lines.append(f'title: "{title}"')
        lines.append(f'first_author: "{first_author}"')
        lines.append(f'year: {year}')
        lines.append(f'journal: "{journal}"')
        lines.append(f'doi: "{doi}"')
        lines.append(f'is_appendix: {str(is_appendix).lower()}')
        
        # 仅附录有 main_paper 字段
        if is_appendix and main_paper:
            lines.append(f'main_paper: "[[{main_paper}]]"')
        elif is_appendix:
            lines.append(f'main_paper: ""')
        
        lines.append(f'status: "{status}"')
        lines.append(f'processed_date: "{processed_date}"')
        lines.append("---")
        
        return "\n".join(lines)
    
    def inject_frontmatter(self, md_content: str, metadata: dict) -> str:
        """将 YAML Frontmatter 注入 Markdown 头部
        
        如果已有 frontmatter（以 --- 开头），替换之；否则在头部插入
        
        Args:
            md_content: 原始 Markdown 内容
            metadata: 元数据字典
            
        Returns:
            str: 注入 frontmatter 后的 Markdown 内容
        """
        frontmatter = self.generate_frontmatter(metadata)
        
        # 检查是否已有 frontmatter
        if md_content.strip().startswith("---"):
            # 找到第二个 --- 的位置
            content_after_first = md_content[3:]
            second_delimiter_pos = content_after_first.find("---")
            
            if second_delimiter_pos != -1:
                # 替换现有的 frontmatter
                rest_content = content_after_first[second_delimiter_pos + 3:]
                # 去除开头的换行符
                rest_content = rest_content.lstrip("\n")
                return f"{frontmatter}\n{rest_content}"
        
        # 没有 frontmatter，在头部插入
        return f"{frontmatter}\n\n{md_content}"
