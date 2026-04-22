"""文本清洗与参考文献双链重构模块"""
import re
from pathlib import Path
from typing import Optional

from src.llm_client import LLMClient
from src.utils import setup_logging, sanitize_filename

logger = setup_logging("text_cleaner")


class TextCleaner:
    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client  # 懒加载，仅 LLM 兜底时才初始化
    
    def _get_llm(self) -> LLMClient:
        if self.llm is None:
            self.llm = LLMClient()
        return self.llm
    
    def clean_markdown(self, md_content: str) -> str:
        """修复 MinerU 输出中的格式问题
        
        处理：
        1. 连续多个空行合并为最多2个空行
        2. 修复断行合并（行末非标点且下一行非标题/列表时合并）
        3. 清理常见乱码字符（如 \x00, \ufffd）
        4. 规范化标题格式（# 后确保有空格）
        5. 移除页眉页脚残留（如纯数字页码行）
        """
        logger.info("开始清洗 Markdown 格式")
        original_length = len(md_content)
        
        # 1. 清理乱码字符
        md_content = md_content.replace('\x00', '')  # 空字符
        md_content = md_content.replace('\ufffd', '')  # 替换字符
        md_content = md_content.replace('\x0b', '')  # 垂直制表符
        md_content = md_content.replace('\x0c', '')  # 换页符
        logger.debug("已清理乱码字符")
        
        # 2. 规范化标题格式（# 后确保有空格）
        md_content = re.sub(r'^(#{1,6})([^\s#])', r'\1 \2', md_content, flags=re.MULTILINE)
        logger.debug("已规范化标题格式")
        
        # 3. 移除页眉页脚残留（纯数字行或数字-数字格式）
        lines = md_content.split('\n')
        cleaned_lines = []
        for line in lines:
            stripped = line.strip()
            # 跳过纯数字行（页码）
            if re.match(r'^\d+$', stripped):
                continue
            # 跳过 "数字 of 数字" 或 "数字 / 数字" 格式
            if re.match(r'^\d+\s+(of|/|/)\s+\d+$', stripped, re.IGNORECASE):
                continue
            cleaned_lines.append(line)
        md_content = '\n'.join(cleaned_lines)
        logger.debug("已移除页眉页脚残留")
        
        # 4. 修复断行合并
        # 行末非标点且下一行非标题/列表/空行时合并
        def merge_line_breaks(text: str) -> str:
            lines = text.split('\n')
            result = []
            i = 0
            while i < len(lines):
                current = lines[i]
                if i + 1 < len(lines):
                    next_line = lines[i + 1]
                    # 检查当前行末尾
                    current_stripped = current.rstrip()
                    # 检查下一行开头
                    next_stripped = next_line.lstrip()
                    
                    # 合并条件：
                    # 1. 当前行末尾不是标点符号（句号、问号、感叹号、冒号、分号）
                    # 2. 下一行不是标题、列表、空行、代码块
                    should_merge = (
                        current_stripped and
                        not current_stripped.endswith(('.', '?', '!', ':', ';', '。', '？', '！', '：', '；')) and
                        next_stripped and
                        not next_stripped.startswith('#') and
                        not re.match(r'^[-*+\d]\s', next_stripped) and
                        not next_stripped.startswith('```') and
                        not next_stripped.startswith('|')  # 表格
                    )
                    
                    if should_merge:
                        result.append(current.rstrip() + ' ' + next_line.lstrip())
                        i += 2
                        continue
                
                result.append(current)
                i += 1
            
            return '\n'.join(result)
        
        md_content = merge_line_breaks(md_content)
        logger.debug("已修复断行合并")
        
        # 5. 连续多个空行合并为最多2个空行
        md_content = re.sub(r'\n{3,}', '\n\n', md_content)
        logger.debug("已合并多余空行")
        
        final_length = len(md_content)
        logger.info(f"Markdown 清洗完成: {original_length} -> {final_length} 字符")
        
        return md_content
    
    def find_references_section(self, md_content: str) -> tuple[str, str]:
        r"""混合策略定位参考文献区域，返回 (main_text, ref_text)
        
        策略 1 - 启发式标题扫描：
        - 从文档末尾向前搜索 "References"、"Bibliography"、"REFERENCES"、
          "参考文献"、"Works Cited"、"Literature Cited" 等标题
        - 匹配模式：^#{1,3}\s*(References|Bibliography|...) 或 ^(References|...)$
        - 找到后，该标题以下的所有内容为参考文献区域
        
        策略 2 - 特征密度验证：
        - 对候选参考文献区域计算特征密度：
          a) 编号模式密度：[1], [2], 1., 2. 等出现频率
          b) 年份模式密度：(1990)-(2026) 四位数年份出现频率  
          c) 期刊标识符密度：Vol., pp., doi:, Journal, Quarterly 等
        - 密度阈值：每100字符中至少有2个以上特征命中
        - 如果候选区域密度不达标，扩大搜索范围或降级到策略3
        
        策略 3 - LLM 兜底：
        - 将文档最后 5000 字符发送给 LLM
        - 要求 LLM 返回 JSON: {"ref_start_text": "参考文献区域的第一行文本"}
        - 用返回的锚点文本在原文中定位切分点
        
        降级策略：
        - 如果三种策略都失败，返回 (md_content, "")，表示无法切分
        """
        logger.info("开始定位参考文献区域")
        
        # 策略 1: 启发式标题扫描
        ref_titles = [
            r'References', r'REFERENCES', r'references',
            r'Bibliography', r'BIBLIOGRAPHY', r'bibliography',
            r'参考文献',
            r'Works\s+Cited', r'works\s+cited', r'WORKS\s+CITED',
            r'Literature\s+Cited', r'literature\s+cited', r'LITERATURE\s+CITED',
            r'References\s+and\s+Notes', r'References\s+and\s+Further\s+Reading'
        ]
        
        # 构建匹配模式
        title_patterns = []
        for title in ref_titles:
            # Markdown 标题格式
            title_patterns.append(rf'^#{{1,3}}\s*{title}\s*$')
            # 纯文本格式（可能带下划线）
            title_patterns.append(rf'^{title}\s*$')
        
        lines = md_content.split('\n')
        candidate_start = -1
        matched_title = ""
        
        # 从后向前搜索
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            for pattern in title_patterns:
                if re.match(pattern, line, re.IGNORECASE):
                    candidate_start = i
                    matched_title = line
                    logger.info(f"策略1找到候选标题: '{matched_title}' 在第 {i+1} 行")
                    break
            if candidate_start != -1:
                break
        
        # 策略 2: 特征密度验证
        if candidate_start != -1:
            ref_text = '\n'.join(lines[candidate_start:])
            density_score = self._calculate_ref_density(ref_text)
            logger.info(f"参考文献区域特征密度: {density_score:.2f} (阈值: 2.0)")
            
            if density_score >= 2.0:
                main_text = '\n'.join(lines[:candidate_start])
                logger.info(f"策略2验证通过，成功切分参考文献区域")
                return (main_text, ref_text)
            else:
                logger.warning(f"特征密度不足，尝试扩大搜索范围")
                # 尝试向上扩展搜索范围
                extended_start = max(0, candidate_start - 20)
                extended_ref_text = '\n'.join(lines[extended_start:])
                extended_density = self._calculate_ref_density(extended_ref_text)
                
                if extended_density >= 2.0:
                    main_text = '\n'.join(lines[:extended_start])
                    logger.info(f"扩大搜索范围后验证通过")
                    return (main_text, extended_ref_text)
                else:
                    logger.warning("扩大搜索范围后仍不足，降级到策略3")
        
        # 策略 3: LLM 兜底
        logger.info("使用 LLM 兜底策略定位参考文献")
        try:
            # 取文档最后 5000 字符
            tail_content = md_content[-5000:] if len(md_content) > 5000 else md_content
            
            system_prompt = """You are an expert at analyzing academic papers. 
Your task is to identify where the references section starts in the given text.
Return a JSON object with the exact first line of the references section."""
            
            user_prompt = f"""Analyze the following text from the end of an academic paper and identify where the references section starts.

The references section typically starts with a heading like "References", "Bibliography", "REFERENCES", "参考文献", "Works Cited", or similar.

Return a JSON object in this exact format:
{{"ref_start_text": "The exact first line of the references section (including the heading)"}}

If you cannot find a references section, return:
{{"ref_start_text": ""}}

Text to analyze:
---
{tail_content}
---"""
            
            result = self._get_llm().chat_completion_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=500
            )
            
            ref_start_text = result.get("ref_start_text", "").strip()
            
            if ref_start_text:
                # 在原文中定位
                ref_pos = md_content.find(ref_start_text)
                if ref_pos != -1:
                    main_text = md_content[:ref_pos]
                    ref_text = md_content[ref_pos:]
                    logger.info(f"LLM 成功定位参考文献区域起始位置: {ref_pos}")
                    return (main_text, ref_text)
                else:
                    # 尝试模糊匹配（去除 Markdown 标记）
                    clean_start = re.sub(r'^#+\s*', '', ref_start_text).strip()
                    for i, line in enumerate(lines):
                        clean_line = re.sub(r'^#+\s*', '', line).strip()
                        if clean_line.lower() == clean_start.lower():
                            main_text = '\n'.join(lines[:i])
                            ref_text = '\n'.join(lines[i:])
                            logger.info(f"模糊匹配成功，参考文献区域起始于第 {i+1} 行")
                            return (main_text, ref_text)
                    
                    logger.warning("LLM 返回的锚点文本无法在原文中定位")
            else:
                logger.warning("LLM 未能识别参考文献区域")
                
        except Exception as e:
            logger.error(f"LLM 兜底策略失败: {e}")
        
        # 降级策略：无法切分
        logger.warning("所有策略均失败，无法切分参考文献区域")
        return (md_content, "")
    
    def _calculate_ref_density(self, text: str) -> float:
        """计算参考文献区域的特征密度
        
        返回: 每100字符的特征命中数
        """
        if not text or len(text) < 100:
            return 0.0
        
        hit_count = 0
        
        # a) 编号模式密度
        number_patterns = [
            r'\[\d+\]',      # [1], [2]
            r'^\d+\.',        # 1., 2. (行首)
            r'^\(\d+\)',     # (1), (2) (行首)
        ]
        for pattern in number_patterns:
            hit_count += len(re.findall(pattern, text, re.MULTILINE))
        
        # b) 年份模式密度 (1990-2026)
        year_pattern = r'\b(19|20)\d{2}\b'
        hit_count += len(re.findall(year_pattern, text))
        
        # c) 期刊标识符密度
        journal_markers = [
            r'Vol\.?', r'vol\.?', r'Volume',
            r'pp\.?', r'pages?', r'Page',
            r'doi[:/]', r'DOI[:/]',
            r'Journal', r'Quarterly', r'Review', r'Economic',
            r'University', r'Press',
            r'et\s+al\.?', r'Ed\.', r'Eds\.'
        ]
        for pattern in journal_markers:
            hit_count += len(re.findall(pattern, text, re.IGNORECASE))
        
        # 计算密度（每100字符）
        density = (hit_count / len(text)) * 100
        return density
    
    def parse_references(self, ref_text: str) -> list[dict]:
        """逐条解析参考文献
        
        步骤：
        1. 多行合并：将物理断行的参考文献合并为单条
           - 以编号模式 [1]、1.、(1) 开头的行作为新条目起始
           - 或以作者名模式（大写字母开头）开始的行
        2. 对每条参考文献提取：
           - authors: 作者列表（尽量提取第一作者姓氏）
           - year: 年份
           - title: 标题
        3. 如果正则提取失败，批量发送给 LLM 提取
           - 一次发送最多 20 条参考文献
           - 要求返回 JSON 数组
        
        返回: list[dict]，每个 dict 包含 {index, raw_text, authors, year, title, bilink_name}
        其中 bilink_name = "{first_author}_{year}_{short_title}" 格式
        """
        logger.info("开始解析参考文献")
        
        if not ref_text.strip():
            logger.warning("参考文献文本为空")
            return []
        
        # 步骤 1: 多行合并
        entries = self._split_reference_entries(ref_text)
        logger.info(f"识别到 {len(entries)} 条参考文献条目")
        
        # 步骤 2: 逐条提取
        parsed_refs = []
        failed_indices = []
        
        for idx, entry in enumerate(entries, 1):
            parsed = self._extract_ref_fields(entry)
            parsed['index'] = idx
            parsed['raw_text'] = entry
            
            if parsed.get('authors') and parsed.get('year') and parsed.get('title'):
                parsed['bilink_name'] = self._generate_bilink_name(parsed)
                parsed_refs.append(parsed)
            else:
                failed_indices.append(idx)
                parsed_refs.append(parsed)  # 仍然添加，但标记为需要 LLM 处理
        
        # 步骤 3: LLM 批量处理失败的条目
        if failed_indices:
            logger.info(f"有 {len(failed_indices)} 条参考文献需要 LLM 处理")
            parsed_refs = self._batch_llm_parse_refs(parsed_refs, failed_indices)
        
        # 为所有成功解析的条目生成 bilink_name
        for ref in parsed_refs:
            if ref.get('authors') and ref.get('year') and not ref.get('bilink_name'):
                ref['bilink_name'] = self._generate_bilink_name(ref)
        
        logger.info(f"参考文献解析完成: {len([r for r in parsed_refs if r.get('bilink_name')])}/{len(parsed_refs)} 条成功")
        return parsed_refs
    
    def _split_reference_entries(self, ref_text: str) -> list[str]:
        """将参考文献文本分割为单条条目"""
        lines = ref_text.split('\n')
        entries = []
        current_entry = []
        
        # 参考文献起始模式
        start_patterns = [
            r'^\[\d+\]',           # [1], [2]
            r'^\d+\.',              # 1., 2.
            r'^\(\d+\)',           # (1), (2)
            r'^\d+\s+',             # 数字 + 空格
            r'^[A-Z][a-z]+,\s+[A-Z]',  # 作者名格式: Smith, J.
            r'^[A-Z][a-z]+\s+[A-Z]\.',  # 作者名格式: Smith J.
        ]
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            
            # 检查是否是新条目的开始
            is_new_entry = False
            for pattern in start_patterns:
                if re.match(pattern, stripped):
                    is_new_entry = True
                    break
            
            if is_new_entry and current_entry:
                # 保存当前条目
                entries.append(' '.join(current_entry))
                current_entry = [stripped]
            else:
                current_entry.append(stripped)
        
        # 保存最后一个条目
        if current_entry:
            entries.append(' '.join(current_entry))
        
        # 清理条目（移除标题行）
        cleaned_entries = []
        title_patterns = [
            r'^(#+\s*)?(References|Bibliography|REFERENCES|参考文献|Works\s+Cited|Literature\s+Cited)',
        ]
        for entry in entries:
            is_title = False
            for pattern in title_patterns:
                if re.match(pattern, entry, re.IGNORECASE):
                    is_title = True
                    break
            if not is_title and len(entry) > 10:  # 过滤掉太短的行
                cleaned_entries.append(entry)
        
        return cleaned_entries
    
    def _extract_ref_fields(self, ref_text: str) -> dict:
        """从单条参考文献中提取字段"""
        result = {
            'authors': [],
            'year': '',
            'title': ''
        }
        
        # 提取年份 (1990-2026)
        year_match = re.search(r'\b(19|20)\d{2}\b', ref_text)
        if year_match:
            result['year'] = year_match.group(0)
        
        # 提取作者
        # 尝试匹配 "Author, A. B." 或 "Author A. B." 或 "Author and Another"
        # 先移除开头的编号前缀如 [1], (1), 1. 等
        ref_text_no_prefix = re.sub(r'^[\[\(]?\d+[\]\)]?\.?\s*', '', ref_text)
        author_patterns = [
            r'^([A-Z][a-zA-Z\-\s]+),\s+[A-Z]',  # Smith, J.
            r'^([A-Z][a-zA-Z\-\s]+)\s+[A-Z]\.',  # Smith J.
            r'^([A-Z][a-z]+)\s+and\s+',  # Smith and
        ]
        
        for pattern in author_patterns:
            match = re.search(pattern, ref_text_no_prefix)
            if match:
                author_str = match.group(1).strip()
                if author_str:
                    result['authors'] = [author_str]
                    break
        
        # 提取标题（在年份之后，期刊名之前）
        if result['year']:
            # 找年份后的引号内容
            title_match = re.search(rf"{result['year']}[,.]?\s*['\"]([^'\"]+)['\"]", ref_text)
            if title_match:
                result['title'] = title_match.group(1)
            else:
                # 尝试找年份后的第一个句子
                after_year = ref_text.split(result['year'], 1)
                if len(after_year) > 1:
                    # 清除年份后的标点符号和空格：).,:;
                    potential_title = after_year[1].lstrip(').,:; ')
                    # 改进标题结束检测：
                    # 第一个句号后面如果跟的是大写字母开头的单词，那才算标题结束
                    # 同时检查期刊标识符作为备选结束标志
                    title_end = None
                    for match in re.finditer(r'\.', potential_title):
                        # 获取句号后的内容
                        after_dot = potential_title[match.end():].lstrip()
                        # 检查句号后是否是大写字母开头（新句子的开始）
                        if after_dot and re.match(r'[A-Z]', after_dot):
                            title_end = match
                            break
                    # 如果没有找到句子边界，尝试期刊标识符
                    if not title_end:
                        title_end = re.search(r'\b(Journal|Review|Quarterly|Vol|pp|doi|DOI)\b', potential_title, re.IGNORECASE)
                    if title_end:
                        result['title'] = potential_title[:title_end.start()].strip('., ')
                    else:
                        result['title'] = potential_title[:100].strip('., ')
        
        return result
    
    def _generate_bilink_name(self, ref: dict) -> str:
        """生成双链名称: {first_author}_{year}_{short_title}"""
        first_author = "unknown"
        if ref.get('authors') and len(ref['authors']) > 0:
            # 提取姓氏
            author = ref['authors'][0]
            # 处理 "Smith, J." 或 "Smith J." 格式
            if ',' in author:
                first_author = author.split(',')[0].strip()
            else:
                first_author = author.split()[0].strip()
            # 清理
            first_author = re.sub(r'[^\w\-]', '', first_author)
        
        year = ref.get('year', 'unknown')
        
        # 短标题：取前3-4个单词
        title = ref.get('title', '')
        short_title = ""
        if title:
            words = title.split()[:10]
            short_title = '_'.join(words)
            short_title = re.sub(r'[^\w\-]', '', short_title)
        
        if not short_title:
            short_title = f"ref{ref.get('index', 0)}"
        
        bilink_name = f"{first_author}_{year}_{short_title}"
        # 清理文件名
        bilink_name = sanitize_filename(bilink_name)
        
        return bilink_name
    
    def _batch_llm_parse_refs(self, parsed_refs: list[dict], failed_indices: list[int]) -> list[dict]:
        """批量使用 LLM 解析失败的参考文献"""
        batch_size = 20
        
        for batch_start in range(0, len(failed_indices), batch_size):
            batch_indices = failed_indices[batch_start:batch_start + batch_size]
            batch_refs = [parsed_refs[i-1] for i in batch_indices if i <= len(parsed_refs)]
            
            if not batch_refs:
                continue
            
            # 构建输入
            refs_input = []
            for ref in batch_refs:
                refs_input.append({
                    "index": ref['index'],
                    "text": ref['raw_text'][:500]  # 限制长度
                })
            
            system_prompt = """You are an expert at parsing academic references.
Extract authors, year, and title from each reference.
Return a JSON array with the extracted information."""
            
            user_prompt = f"""Parse the following academic references and extract the authors, year, and title for each.

Return a JSON array in this exact format:
[
  {{
    "index": 1,
    "authors": ["Author1", "Author2"],
    "year": "2024",
    "title": "Paper Title"
  }},
  ...
]

If you cannot extract a field, use an empty string or empty array.

References to parse:
{refs_input}
"""
            
            try:
                result = self._get_llm().chat_completion_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.1,
                    max_tokens=2000
                )
                
                if isinstance(result, list):
                    for item in result:
                        idx = item.get('index')
                        if idx and 1 <= idx <= len(parsed_refs):
                            ref = parsed_refs[idx - 1]
                            if item.get('authors'):
                                ref['authors'] = item['authors'] if isinstance(item['authors'], list) else [item['authors']]
                            if item.get('year'):
                                ref['year'] = str(item['year'])
                            if item.get('title'):
                                ref['title'] = item['title']
                
                logger.info(f"LLM 成功解析批次: {batch_indices}")
                
            except Exception as e:
                logger.error(f"LLM 批量解析失败: {e}")
        
        return parsed_refs
    
    def rebuild_bilinks(self, main_text: str, refs: list[dict]) -> str:
        """将正文中的引用标记替换为 Obsidian 双链格式
        
        处理的引用格式：
        1. 方括号编号: [1], [2,3], [1-5], [1, 2, 3]
        2. 作者-年份: (Author, 2024), (Author et al., 2024)
        
        替换逻辑：
        - [1] -> [[bilink_name_of_ref_1]]
        - [1,2] -> [[bilink_1]], [[bilink_2]]
        - [1-3] -> [[bilink_1]], [[bilink_2]], [[bilink_3]]
        - (Author, 2024) -> [[Author_2024_title]]（如果能在 refs 中匹配到）
        
        注意：不要替换 Markdown 链接中的方括号 [text](url)
        注意：不要替换已有的 ![[image]] 引用
        """
        logger.info("开始重建双链引用")
        
        if not refs:
            logger.warning("参考文献列表为空，跳过双链替换")
            return main_text
        
        # 构建索引到 bilink_name 的映射
        index_to_bilink = {}
        author_year_to_bilink = {}
        
        for ref in refs:
            idx = ref.get('index')
            bilink = ref.get('bilink_name', '')
            if idx and bilink:
                index_to_bilink[idx] = bilink
            
            # 构建作者-年份映射
            if ref.get('authors') and ref.get('year'):
                first_author = ref['authors'][0].split()[0] if ref['authors'] else ''
                first_author = re.sub(r'[^\w]', '', first_author)
                year = ref['year']
                key = f"{first_author.lower()}_{year}"
                author_year_to_bilink[key] = bilink
        
        logger.debug(f"构建了 {len(index_to_bilink)} 条编号映射和 {len(author_year_to_bilink)} 条作者-年份映射")
        
        result = main_text
        
        # 1. 处理方括号编号引用 [1], [1,2], [1-3], [1, 2, 3]
        def replace_bracket_citation(match):
            content = match.group(1)
            
            # 检查是否是 Markdown 链接的一部分 [text](url)
            # 通过检查后面是否紧跟 ( 来判断
            pos = match.end()
            if pos < len(result) and result[pos] == '(':
                return match.group(0)  # 不替换
            
            # 解析引用编号
            citations = []
            
            # 处理范围 [1-5]
            if '-' in content and ',' not in content:
                try:
                    start, end = content.split('-')
                    start = int(start.strip())
                    end = int(end.strip())
                    citations = list(range(start, end + 1))
                except ValueError:
                    citations = [content]
            # 处理列表 [1,2,3] 或 [1, 2, 3]
            elif ',' in content:
                try:
                    citations = [int(x.strip()) for x in content.split(',')]
                except ValueError:
                    citations = [content]
            else:
                # 单个引用
                try:
                    citations = [int(content.strip())]
                except ValueError:
                    return match.group(0)  # 不是数字，不替换
            
            # 构建双链
            bilinks = []
            for c in citations:
                if c in index_to_bilink:
                    bilinks.append(f"[[{index_to_bilink[c]}]]")
                else:
                    # 如果找不到对应的参考文献，保留原样
                    logger.debug(f"引用 [{c}] 未找到对应参考文献")
            
            if bilinks:
                return ', '.join(bilinks)
            else:
                return match.group(0)  # 没有找到对应项，保留原样
        
        # 匹配方括号引用，但要排除 ![...] 图片引用
        # 使用负向前瞻确保不是 ![ 开头
        result = re.sub(r'(?<!!)\[(\d+(?:[-,]\s*\d+)*)\]', replace_bracket_citation, result)
        
        # 2. 处理作者-年份引用 (Author, 2024), (Author et al., 2024)
        def replace_author_year_citation(match):
            author_part = match.group(1)
            year = match.group(2)
            
            # 提取第一作者姓氏
            first_author = author_part.split(',')[0].split()[-1]  # 取最后一个词（姓氏）
            first_author = re.sub(r'[^\w]', '', first_author).lower()
            
            key = f"{first_author}_{year}"
            
            if key in author_year_to_bilink:
                return f"([[{author_year_to_bilink[key]}]])"
            else:
                # 尝试模糊匹配
                for k, v in author_year_to_bilink.items():
                    if k.endswith(f"_{year}"):
                        return f"([[{v}]])"
            
            return match.group(0)  # 没有找到对应项，保留原样
        
        # 匹配 (Author, 2024) 或 (Author et al., 2024) 格式
        result = re.sub(
            r'\(([A-Z][a-zA-Z\s,]+(?:et\s+al\.)?),?\s*(\d{4})\)',
            replace_author_year_citation,
            result
        )
        
        logger.info("双链引用重建完成")
        return result
    
    def build_references_section(self, refs: list[dict]) -> str:
        """重建参考文献区域，用双链格式
        
        输出格式：
        ## References
        
        1. [[Author1_2024_Title1]]
        2. [[Author2_2023_Title2]]
        ...
        
        每条保留原始文本作为注释或描述
        """
        logger.info("开始重建参考文献区域")
        
        if not refs:
            logger.warning("参考文献列表为空")
            return ""
        
        lines = ["## References", ""]
        
        for ref in refs:
            idx = ref.get('index', 0)
            bilink = ref.get('bilink_name', '')
            raw_text = ref.get('raw_text', '')
            
            if bilink:
                lines.append(f"{idx}. [[{bilink}]]")
                # 保留原始文本作为注释（缩进）
                if raw_text:
                    # 截断过长的原始文本
                    display_text = raw_text[:200] + "..." if len(raw_text) > 200 else raw_text
                    lines.append(f"   > {display_text}")
            else:
                # 没有 bilink_name，保留原始文本
                lines.append(f"{idx}. {raw_text[:150]}...")
        
        lines.append("")
        logger.info(f"参考文献区域重建完成: {len(refs)} 条")
        
        return '\n'.join(lines)
    
    def process(self, md_content: str) -> tuple[str, str, list[dict]]:
        """完整的文本清洗和双链重构流程
        
        返回: (processed_md, ref_status, refs)
        - processed_md: 处理后的完整 Markdown（含正文双链替换 + 重建的参考文献区域）
        - ref_status: "success" | "partial" | "raw"
          - success: 成功切分并替换
          - partial: 切分成功但部分引用未能替换
          - raw: 无法切分，保留原文
        - refs: 解析出的参考文献列表
        """
        logger.info("=" * 50)
        logger.info("开始文本清洗和双链重构流程")
        
        # 步骤 1: 清洗 Markdown
        cleaned_md = self.clean_markdown(md_content)
        
        # 步骤 2: 定位参考文献区域
        main_text, ref_text = self.find_references_section(cleaned_md)
        
        if not ref_text:
            logger.warning("无法切分参考文献区域，返回原始文本")
            return (cleaned_md, "raw", [])
        
        # 步骤 3: 解析参考文献
        refs = self.parse_references(ref_text)
        
        if not refs:
            logger.warning("未能解析出任何参考文献")
            return (cleaned_md, "raw", [])
        
        # 步骤 4: 重建正文双链
        main_text_with_bilinks = self.rebuild_bilinks(main_text, refs)
        
        # 步骤 5: 重建参考文献区域
        new_ref_section = self.build_references_section(refs)
        
        # 合并结果
        processed_md = main_text_with_bilinks + '\n\n' + new_ref_section
        
        # 判断状态
        successful_refs = len([r for r in refs if r.get('bilink_name')])
        total_refs = len(refs)
        
        if successful_refs == total_refs:
            ref_status = "success"
        elif successful_refs > 0:
            ref_status = "partial"
        else:
            ref_status = "raw"
        
        logger.info(f"处理完成: status={ref_status}, refs={successful_refs}/{total_refs}")
        logger.info("=" * 50)
        
        return (processed_md, ref_status, refs)
