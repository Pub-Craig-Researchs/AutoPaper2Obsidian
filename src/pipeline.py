"""主调度管线 - 串联全流程"""
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

import json

from src.config import PAPERS_RAW, TEMP_OUTPUT_DIR, MAPPING_LOG
from src.utils import setup_logging, ensure_dirs
from src.mineru_client import MineruClient
from src.llm_client import LLMClient
from src.metadata_extractor import MetadataExtractor
from src.text_cleaner import TextCleaner
from src.archiver import Archiver

logger = setup_logging("pipeline")


@dataclass
class ProcessResult:
    """单篇论文处理结果"""
    source_pdf: str
    paper_name: str = ""
    status: str = "pending"  # pending | success | partial | failed
    ref_status: str = ""     # success | partial | raw
    images_count: int = 0
    error: str = ""
    metadata: dict = field(default_factory=dict)


class Pipeline:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.mineru = MineruClient()
        self.llm = LLMClient()
        self.extractor = MetadataExtractor(self.llm)
        self.cleaner = TextCleaner(self.llm)
        self.archiver = Archiver()
        ensure_dirs()
    
    def process_single(self, pdf_path: Path) -> ProcessResult:
        """单篇论文全流程处理
        
        步骤：
        1. 调用 MinerU 解析 PDF -> Markdown + images
        2. 读取 Markdown 内容
        3. 调用 LLM 提取元数据 -> 生成标准文件名
        4. 文本清洗 + 参考文献双链重构
        5. YAML Frontmatter 注入
        6. 如果不是 dry_run：
           a. 图片迁移 + 路径重写
           b. Markdown 归档到 Obsidian
           c. PDF 归档到 legacy
           d. 写入映射日志
        
        错误处理：
        - 每个步骤用 try/except 包装
        - MinerU 解析失败 -> 记录错误，返回 failed
        - 元数据提取失败 -> 使用时间戳作为文件名，标记 manual_review
        - 文本清洗失败 -> 保留原文，标记 ref_status=raw
        - 归档失败 -> 记录错误，返回 partial
        """
        result = ProcessResult(source_pdf=pdf_path.name)
        
        try:
            # Step 1: MinerU 解析
            logger.info(f"[1/8] 开始解析: {pdf_path.name}")
            md_dir, images_dir = self.mineru.parse_pdf(pdf_path)
            
            # Step 2: 读取 Markdown
            logger.info(f"[2/8] 读取 Markdown")
            md_content = self.mineru.read_markdown(md_dir)
            if not md_content:
                raise ValueError("Markdown 内容为空")
            
            # Step 3: 提取元数据
            logger.info(f"[3/8] 提取元数据")
            try:
                metadata = self.extractor.extract_metadata(md_content)
                paper_name = self.extractor.generate_standard_filename(metadata)
                result.metadata = metadata
            except Exception as e:
                logger.warning(f"元数据提取失败，使用时间戳命名: {e}")
                paper_name = f"unknown_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{pdf_path.stem}"
                metadata = {"title": pdf_path.stem, "status": "manual_review"}
                result.metadata = metadata
            
            result.paper_name = paper_name
            
            # Step 4: 文本清洗 + 双链重构
            logger.info(f"[4/8] 文本清洗与双链重构")
            try:
                md_content, ref_status, refs = self.cleaner.process(md_content)
                result.ref_status = ref_status
            except Exception as e:
                logger.warning(f"文本清洗失败，保留原文: {e}")
                result.ref_status = "raw"
            
            # Step 5: 注入 Frontmatter
            logger.info(f"[5/8] 注入 YAML Frontmatter")
            md_content = self.extractor.inject_frontmatter(md_content, metadata)
            
            if self.dry_run:
                logger.info(f"[DRY-RUN] 跳过归档步骤")
                result.status = "success"
                return result
            
            # Step 6-8: 归档
            logger.info(f"[6-8/8] 归档迁移")
            archive_result = self.archiver.archive_single(
                pdf_path=pdf_path,
                md_content=md_content,
                images_dir=images_dir,
                paper_name=paper_name
            )
            result.images_count = archive_result.get("images_count", 0)
            result.status = archive_result.get("status", "success")
            
        except Exception as e:
            logger.error(f"处理失败 [{pdf_path.name}]: {e}")
            result.status = "failed"
            result.error = str(e)
        
        return result
    
    @staticmethod
    def _load_processed_pdfs() -> set[str]:
        """从 mapping_log.jsonl 读取已成功处理的 source_pdf 集合"""
        processed = set()
        if not MAPPING_LOG.exists():
            return processed
        try:
            with open(MAPPING_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if "source_pdf" in record:
                            processed.add(record["source_pdf"])
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.warning(f"读取映射日志失败: {e}")
        return processed

    def scan_pdfs(self, single_file: str = None) -> list[Path]:
        """扫描待处理的 PDF 文件，自动跳过已成功处理的文件"""
        if single_file:
            p = Path(single_file)
            if not p.is_absolute():
                p = PAPERS_RAW / p
            if p.exists():
                return [p]
            else:
                logger.error(f"文件不存在: {p}")
                return []
        
        all_pdfs = sorted(PAPERS_RAW.glob("*.pdf"))
        processed = self._load_processed_pdfs()
        
        if processed:
            pdfs = [p for p in all_pdfs if p.name not in processed]
            skipped = len(all_pdfs) - len(pdfs)
            logger.info(f"扫描到 {len(all_pdfs)} 个 PDF，跳过 {skipped} 个已处理，剩余 {len(pdfs)} 个待处理")
        else:
            pdfs = all_pdfs
            logger.info(f"扫描到 {len(pdfs)} 个 PDF 文件")
        
        return pdfs


def run_pipeline(dry_run: bool = False, single_file: str = None):
    """主入口函数 - 供 run.py 调用"""
    pipeline = Pipeline(dry_run=dry_run)
    pdfs = pipeline.scan_pdfs(single_file)
    
    if not pdfs:
        logger.warning("没有找到待处理的 PDF 文件")
        return
    
    results = []
    for i, pdf_path in enumerate(pdfs, 1):
        logger.info(f"===== [{i}/{len(pdfs)}] 处理: {pdf_path.name} =====")
        result = pipeline.process_single(pdf_path)
        results.append(result)
        logger.info(f"结果: {result.status} -> {result.paper_name}")
    
    # 汇总统计
    success = sum(1 for r in results if r.status == "success")
    partial = sum(1 for r in results if r.status == "partial")
    failed = sum(1 for r in results if r.status == "failed")
    
    logger.info("=" * 50)
    logger.info(f"处理完成: 总计 {len(results)} 篇")
    logger.info(f"  成功: {success}")
    logger.info(f"  部分成功: {partial}")
    logger.info(f"  失败: {failed}")
    
    if failed > 0:
        logger.warning("失败的文件:")
        for r in results:
            if r.status == "failed":
                logger.warning(f"  - {r.source_pdf}: {r.error}")
