"""归档与迁移模块 - 图片迁移、Markdown 归档、PDF 重命名"""
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from src.config import OBSIDIAN_VAULT, OBSIDIAN_ATTACHMENTS, PAPERS_LEGACY, MAPPING_LOG, LOG_DIR
from src.utils import setup_logging, safe_move, ensure_dirs

# 支持的图片格式
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# 延迟初始化的 logger
_logger = None


def get_logger():
    """获取 logger 实例，延迟初始化以确保目录已创建"""
    global _logger
    if _logger is None:
        # 确保日志目录存在
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _logger = setup_logging("archiver")
    return _logger


class Archiver:
    def __init__(self):
        ensure_dirs()
    
    def migrate_images(self, images_dir: Path, paper_name: str) -> dict:
        """将 MinerU 输出的图片复制到 Obsidian attachments
        
        目标路径: E:\\Obsidian\\AutoPaperArrange\\attachments\\{paper_name}\\
        
        Args:
            images_dir: MinerU 解析输出的 images/ 目录
            paper_name: 标准化的论文名（如 Zhang_2024_Deep_Learning）
            
        Returns:
            dict: 路径映射 {原始相对路径: Obsidian 附件路径}
            例如 {"images/fig1.png": "attachments/Zhang_2024_Deep_Learning/fig1.png"}
        """
        logger = get_logger()
        path_map = {}
        
        if not images_dir.exists():
            logger.warning(f"图片目录不存在: {images_dir}")
            return path_map
        
        # 创建目标目录
        target_dir = OBSIDIAN_ATTACHMENTS / paper_name
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"创建目标目录: {target_dir}")
        except Exception as e:
            logger.error(f"创建目标目录失败: {target_dir}, 错误: {e}")
            return path_map
        
        # 遍历 images_dir 中所有图片文件
        try:
            for img_file in images_dir.iterdir():
                if img_file.is_file() and img_file.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
                    try:
                        # 复制到目标目录
                        dest_file = target_dir / img_file.name
                        
                        # 如果目标文件已存在，添加数字后缀
                        counter = 1
                        original_dest = dest_file
                        while dest_file.exists():
                            stem = original_dest.stem
                            suffix = original_dest.suffix
                            dest_file = target_dir / f"{stem}_{counter}{suffix}"
                            counter += 1
                        
                        shutil.copy2(img_file, dest_file)
                        logger.info(f"复制图片: {img_file.name} -> {dest_file}")
                        
                        # 构建路径映射
                        # 原始相对路径格式: images/filename.ext
                        original_rel_path = f"images/{img_file.name}"
                        original_rel_path_alt = f"./images/{img_file.name}"
                        
                        # Obsidian 附件路径（用于日志记录）
                        obsidian_rel_path = f"attachments/{paper_name}/{dest_file.name}"
                        
                        path_map[original_rel_path] = obsidian_rel_path
                        path_map[original_rel_path_alt] = obsidian_rel_path
                        # 同时存储文件名映射，用于重写路径
                        path_map[img_file.name] = dest_file.name
                        
                    except Exception as e:
                        logger.error(f"复制图片失败: {img_file}, 错误: {e}")
                        continue
        except Exception as e:
            logger.error(f"遍历图片目录失败: {images_dir}, 错误: {e}")
        
        logger.info(f"共迁移 {len(path_map) // 3} 张图片到 {target_dir}")
        return path_map
    
    def rewrite_image_paths(self, md_content: str, path_map: dict) -> str:
        """重写 Markdown 中的图片引用路径为 Obsidian 格式
        
        将 ![alt](images/fig1.png) 或 ![alt](./images/fig1.png)
        替换为 ![[fig1.png]]（Obsidian wiki-link 格式）
        
        同时处理已有的 ![[image_name]] 格式（可能需要添加子目录前缀）
        
        Args:
            md_content: 原始 Markdown 内容
            path_map: 路径映射字典，包含原始路径到新文件名的映射
            
        Returns:
            str: 重写后的 Markdown 内容
        """
        if not path_map:
            return md_content
        
        result = md_content
        
        # 匹配标准 Markdown 图片语法: ![alt](path)
        # 支持以下格式:
        # - ![alt](images/fig1.png)
        # - ![alt](./images/fig1.png)
        # - ![alt](images/fig1.png "title")
        md_image_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
        
        def replace_md_image(match):
            alt_text = match.group(1)
            path_with_title = match.group(2)
            
            # 提取路径（去掉可能的 title）
            path_parts = path_with_title.split('"')
            img_path = path_parts[0].strip()
            
            # 获取文件名
            img_filename = Path(img_path).name
            
            # 检查是否在映射中
            if img_path in path_map:
                # 使用映射中的新文件名
                new_filename = Path(path_map[img_path]).name
                return f"![[{new_filename}]]"
            elif img_filename in path_map:
                # 直接使用文件名映射
                new_filename = path_map[img_filename]
                return f"![[{new_filename}]]"
            else:
                # 未找到映射，保持原样
                return match.group(0)
        
        result = re.sub(md_image_pattern, replace_md_image, result)
        
        # 处理已有的 Obsidian wiki-link 格式: ![[filename]]
        # 如果文件名在 path_map 中有映射，更新为新文件名
        wiki_link_pattern = r'!\[\[([^\]]+)\]\]'
        
        def replace_wiki_link(match):
            filename = match.group(1)
            # 移除可能的路径前缀
            basename = Path(filename).name
            
            if basename in path_map:
                new_filename = path_map[basename]
                return f"![[{new_filename}]]"
            elif filename in path_map:
                new_filename = path_map[filename]
                return f"![[{new_filename}]]"
            else:
                return match.group(0)
        
        result = re.sub(wiki_link_pattern, replace_wiki_link, result)
        
        return result
    
    def archive_markdown(self, md_content: str, paper_name: str) -> Path:
        """将最终 Markdown 写入 Obsidian 库根目录
        
        目标路径: E:\\Obsidian\\AutoPaperArrange\\{paper_name}.md
        
        事务性保证：先写 .tmp 文件，成功后 rename
        
        Args:
            md_content: Markdown 内容
            paper_name: 标准化论文名
            
        Returns:
            Path: 最终文件路径
        """
        logger = get_logger()
        target_path = OBSIDIAN_VAULT / f"{paper_name}.md"
        temp_path = OBSIDIAN_VAULT / f"{paper_name}.md.tmp"
        
        try:
            # 确保目标目录存在
            OBSIDIAN_VAULT.mkdir(parents=True, exist_ok=True)
            
            # 先写入临时文件
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(md_content)
            
            logger.info(f"临时文件写入成功: {temp_path}")
            
            # 如果目标文件已存在，先删除
            if target_path.exists():
                try:
                    target_path.unlink()
                    logger.info(f"删除已存在的文件: {target_path}")
                except Exception as e:
                    logger.error(f"删除已存在文件失败: {target_path}, 错误: {e}")
                    raise
            
            # 重命名临时文件为最终文件
            temp_path.rename(target_path)
            logger.info(f"Markdown 归档成功: {target_path}")
            
            return target_path
            
        except Exception as e:
            logger.error(f"Markdown 归档失败: {target_path}, 错误: {e}")
            # 清理临时文件
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except:
                    pass
            raise
    
    def archive_pdf(self, pdf_path: Path, paper_name: str) -> Path:
        """将原始 PDF 重命名并移动到 papers_legacy
        
        目标: papers_legacy\\{paper_name}.pdf
        使用 safe_move 处理同名冲突
        
        Args:
            pdf_path: 原始 PDF 路径
            paper_name: 标准化论文名
            
        Returns:
            Path: 最终文件路径
        """
        logger = get_logger()
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF 文件不存在: {pdf_path}")
        
        target_path = PAPERS_LEGACY / f"{paper_name}.pdf"
        
        try:
            # 使用 safe_move 处理同名冲突
            final_path = safe_move(pdf_path, target_path)
            logger.info(f"PDF 归档成功: {pdf_path} -> {final_path}")
            return final_path
            
        except Exception as e:
            logger.error(f"PDF 归档失败: {pdf_path} -> {target_path}, 错误: {e}")
            raise
    
    def write_mapping_log(self, record: dict) -> None:
        """追加写入 JSON Lines 格式的处理日志
        
        record 应包含：
        - timestamp: ISO 格式时间
        - source_pdf: 原始 PDF 文件名
        - paper_name: 标准化名称
        - obsidian_md: Obsidian 中的 Markdown 路径
        - legacy_pdf: legacy 中的 PDF 路径
        - images_count: 迁移的图片数量
        - status: "success" | "partial" | "failed"
        - error: 错误信息（如有）
        
        写入到 logs/mapping_log.jsonl
        
        Args:
            record: 日志记录字典
        """
        logger = get_logger()
        # 确保日志目录存在
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        
        # 添加时间戳（如果没有）
        if "timestamp" not in record:
            record["timestamp"] = datetime.now().isoformat()
        
        # 将 Path 对象转换为字符串
        for key, value in record.items():
            if isinstance(value, Path):
                record[key] = str(value)
        
        try:
            with open(MAPPING_LOG, "a", encoding="utf-8") as f:
                json_line = json.dumps(record, ensure_ascii=False)
                f.write(json_line + "\n")
            logger.info(f"映射日志已写入: {MAPPING_LOG}")
        except Exception as e:
            logger.error(f"写入映射日志失败: {e}")
            raise
    
    def archive_single(self, pdf_path: Path, md_content: str, images_dir: Path, paper_name: str) -> dict:
        """一站式归档：图片迁移 + 路径重写 + Markdown 归档 + PDF 归档 + 日志
        
        这是对外的主要接口，组合上述所有操作。
        错误处理：任何步骤失败都记录到日志，但尽量完成其他步骤。
        
        Args:
            pdf_path: 原始 PDF 路径
            md_content: Markdown 内容
            images_dir: 图片目录路径
            paper_name: 标准化论文名
            
        Returns:
            dict: {
                "paper_name": str,
                "obsidian_md": Path,
                "legacy_pdf": Path,
                "images_count": int,
                "status": "success" | "partial" | "failed",
                "error": str | None
            }
        """
        logger = get_logger()
        result = {
            "paper_name": paper_name,
            "obsidian_md": None,
            "legacy_pdf": None,
            "images_count": 0,
            "status": "success",
            "error": None
        }
        
        errors = []
        
        # 步骤 1: 图片迁移
        path_map = {}
        try:
            path_map = self.migrate_images(images_dir, paper_name)
            result["images_count"] = len(path_map) // 3  # 每个图片有3个映射条目
            logger.info(f"图片迁移完成: {result['images_count']} 张")
        except Exception as e:
            error_msg = f"图片迁移失败: {e}"
            logger.error(error_msg)
            errors.append(error_msg)
            result["status"] = "partial"
        
        # 步骤 2: 重写 Markdown 图片路径
        rewritten_md = md_content
        try:
            rewritten_md = self.rewrite_image_paths(md_content, path_map)
            logger.info("Markdown 图片路径重写完成")
        except Exception as e:
            error_msg = f"Markdown 路径重写失败: {e}"
            logger.error(error_msg)
            errors.append(error_msg)
            result["status"] = "partial"
        
        # 步骤 3: 归档 Markdown
        try:
            md_path = self.archive_markdown(rewritten_md, paper_name)
            result["obsidian_md"] = md_path
            logger.info(f"Markdown 归档完成: {md_path}")
        except Exception as e:
            error_msg = f"Markdown 归档失败: {e}"
            logger.error(error_msg)
            errors.append(error_msg)
            result["status"] = "failed"
        
        # 步骤 4: 归档 PDF
        try:
            legacy_path = self.archive_pdf(pdf_path, paper_name)
            result["legacy_pdf"] = legacy_path
            logger.info(f"PDF 归档完成: {legacy_path}")
        except Exception as e:
            error_msg = f"PDF 归档失败: {e}"
            logger.error(error_msg)
            errors.append(error_msg)
            result["status"] = "failed"
        
        # 如果有错误，更新状态和信息
        if errors:
            result["error"] = "; ".join(errors)
            if result["status"] == "success":
                result["status"] = "partial"
        
        # 步骤 5: 写入映射日志
        log_record = {
            "source_pdf": pdf_path.name,
            "paper_name": paper_name,
            "obsidian_md": result["obsidian_md"],
            "legacy_pdf": result["legacy_pdf"],
            "images_count": result["images_count"],
            "status": result["status"],
            "error": result["error"]
        }
        
        try:
            self.write_mapping_log(log_record)
        except Exception as e:
            logger.error(f"写入映射日志失败: {e}")
        
        logger.info(f"归档完成: {paper_name}, 状态: {result['status']}")
        return result
