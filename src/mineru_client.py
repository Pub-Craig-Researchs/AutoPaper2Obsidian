"""MinerU API 客户端封装"""
import time
import zipfile
import io
from pathlib import Path
from typing import Optional
import httpx

from src.config import MINERU_API, TEMP_OUTPUT_DIR
from src.utils import setup_logging, retry_with_backoff

logger = setup_logging("mineru_client")


class MineruClientError(RuntimeError):
    """MinerU 客户端错误"""
    pass


class MineruClient:
    """MinerU PDF 解析服务客户端"""

    def __init__(self, base_url: str = None, timeout: float = 4800.0):
        self.base_url = (base_url or MINERU_API).rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def _make_url(self, path: str) -> str:
        """构建完整 URL"""
        return f"{self.base_url}{path}"

    def check_health(self) -> dict:
        """检查 MinerU 服务是否在线
        
        Returns:
            健康检查响应字典，如 {"status": "ok", "service": "paper-miner"}
        """
        url = self._make_url("/health")
        try:
            logger.info(f"检查 MinerU 服务健康状态: {url}")
            response = self._client.get(url)
            response.raise_for_status()
            result = response.json()
            logger.info(f"MinerU 服务状态: {result.get('status', 'unknown')}")
            return result
        except httpx.HTTPStatusError as e:
            logger.error(f"健康检查 HTTP 错误: {e.response.status_code} - {e.response.text}")
            raise MineruClientError(f"健康检查失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"健康检查请求错误: {e}")
            raise MineruClientError(f"无法连接到 MinerU 服务: {e}") from e
        except Exception as e:
            logger.error(f"健康检查未知错误: {e}")
            raise MineruClientError(f"健康检查失败: {e}") from e

    def submit_pdf(self, pdf_path: Path) -> str:
        """异步提交 PDF 解析任务，返回 task_id
        
        Args:
            pdf_path: PDF 文件路径
            
        Returns:
            任务 ID (task_id)
            
        Raises:
            MineruClientError: 提交失败时抛出
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise MineruClientError(f"PDF 文件不存在: {pdf_path}")
        
        if not pdf_path.suffix.lower() == ".pdf":
            raise MineruClientError(f"文件不是 PDF: {pdf_path}")
        
        url = self._make_url("/tasks")
        
        try:
            logger.info(f"提交 PDF 解析任务: {pdf_path.name}")
            
            with open(pdf_path, "rb") as f:
                files = [
                    ("files", (pdf_path.name, f, "application/pdf"))
                ]
                data = {
                    "response_format_zip": "true",
                    "return_images": "true",
                    "return_md": "true",
                    "backend": "hybrid-auto-engine",
                }
                response = self._client.post(url, files=files, data=data)
            
            if response.status_code == 202:
                result = response.json()
                task_id = result.get("task_id")
                if task_id:
                    logger.info(f"任务提交成功，task_id: {task_id}")
                    return task_id
                else:
                    raise MineruClientError(f"响应中缺少 task_id: {result}")
            else:
                logger.error(f"提交失败: HTTP {response.status_code} - {response.text}")
                raise MineruClientError(f"提交 PDF 失败: HTTP {response.status_code}")
                
        except httpx.HTTPStatusError as e:
            logger.error(f"提交 PDF HTTP 错误: {e.response.status_code} - {e.response.text}")
            raise MineruClientError(f"提交 PDF 失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"提交 PDF 请求错误: {e}")
            raise MineruClientError(f"提交 PDF 请求失败: {e}") from e
        except Exception as e:
            logger.error(f"提交 PDF 未知错误: {e}")
            raise MineruClientError(f"提交 PDF 失败: {e}") from e

    def poll_status(self, task_id: str, poll_interval: float = 60.0, max_wait: float = 4800.0) -> dict:
        """轮询任务状态直到完成或失败
        
        Args:
            task_id: 任务 ID
            poll_interval: 轮询间隔（秒）
            max_wait: 最大等待时间（秒）
            
        Returns:
            状态字典，包含 status、task_id、download_url 等
            
        Raises:
            MineruClientError: 任务失败或超时
        """
        url = self._make_url(f"/tasks/{task_id}")
        start_time = time.time()
        
        logger.info(f"开始轮询任务状态: {task_id}")
        
        while True:
            try:
                response = self._client.get(url)
                
                if response.status_code == 404:
                    logger.error(f"任务不存在: {task_id}")
                    raise MineruClientError(f"任务不存在: {task_id}")
                
                response.raise_for_status()
                result = response.json()
                status = result.get("status")
                
                if status == "completed":
                    logger.info(f"任务完成: {task_id}")
                    return result
                elif status == "failed":
                    error_msg = result.get("error", "未知错误")
                    logger.error(f"任务失败: {task_id}, 错误: {error_msg}")
                    raise MineruClientError(f"任务失败: {error_msg}")
                elif status == "processing":
                    elapsed = time.time() - start_time
                    if elapsed > max_wait:
                        logger.error(f"任务轮询超时: {task_id}, 已等待 {elapsed:.1f} 秒")
                        raise MineruClientError(f"任务轮询超时，已等待 {elapsed:.1f} 秒")
                    
                    logger.debug(f"任务处理中: {task_id}, 已等待 {elapsed:.1f} 秒")
                    time.sleep(poll_interval)
                else:
                    logger.warning(f"未知任务状态: {status}")
                    time.sleep(poll_interval)
                    
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 500:
                    # 500 可能是任务失败
                    try:
                        error_result = e.response.json()
                        if error_result.get("status") == "failed":
                            error_msg = error_result.get("error", "未知错误")
                            logger.error(f"任务失败 (HTTP 500): {task_id}, 错误: {error_msg}")
                            raise MineruClientError(f"任务失败: {error_msg}")
                    except:
                        pass
                logger.error(f"轮询状态 HTTP 错误: {e.response.status_code}")
                raise MineruClientError(f"轮询状态失败: HTTP {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error(f"轮询状态请求错误: {e}")
                raise MineruClientError(f"轮询状态请求失败: {e}") from e
            except MineruClientError:
                raise
            except Exception as e:
                logger.error(f"轮询状态未知错误: {e}")
                raise MineruClientError(f"轮询状态失败: {e}") from e

    def download_result(self, task_id: str, output_dir: Path = None) -> tuple[Path, Path]:
        """下载并解压解析结果
        
        Args:
            task_id: 任务 ID
            output_dir: 输出目录，默认为 TEMP_OUTPUT_DIR / task_id
            
        Returns:
            (md_dir, images_dir) 元组
            
        Raises:
            MineruClientError: 下载或解压失败
        """
        if output_dir is None:
            output_dir = TEMP_OUTPUT_DIR / task_id
        else:
            output_dir = Path(output_dir)
        
        url = self._make_url(f"/tasks/{task_id}/result")
        
        try:
            logger.info(f"下载解析结果: {task_id}")
            response = self._client.get(url)
            response.raise_for_status()
            
            # 解压 ZIP
            output_dir.mkdir(parents=True, exist_ok=True)
            
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                zf.extractall(output_dir)
            
            logger.info(f"解压完成: {output_dir}")
            
            # MinerU ZIP 结构: {pdf_stem}/{backend}/*.md + {pdf_stem}/{backend}/images/
            # 递归查找第一个 .md 文件所在目录作为 md_dir
            md_dir = None
            images_dir = None
            
            # 查找包含 .md 文件的目录
            md_files = list(output_dir.rglob("*.md"))
            if md_files:
                md_dir = md_files[0].parent
                # images 通常与 .md 同级
                potential_images = md_dir / "images"
                if potential_images.exists():
                    images_dir = potential_images
            
            # 回退：查找 images 目录
            if images_dir is None:
                img_dirs = list(output_dir.rglob("images"))
                if img_dirs:
                    images_dir = img_dirs[0]
            
            # 如果仍未找到，使用默认路径
            if md_dir is None:
                md_dir = output_dir / "md"
            if images_dir is None:
                images_dir = output_dir / "images"
            
            logger.info(f"结果目录: md={md_dir.exists()}, images={images_dir.exists()}")
            return md_dir, images_dir
            
        except httpx.HTTPStatusError as e:
            logger.error(f"下载结果 HTTP 错误: {e.response.status_code}")
            raise MineruClientError(f"下载结果失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"下载结果请求错误: {e}")
            raise MineruClientError(f"下载结果请求失败: {e}") from e
        except zipfile.BadZipFile as e:
            logger.error(f"ZIP 文件损坏: {e}")
            raise MineruClientError(f"ZIP 文件损坏: {e}") from e
        except Exception as e:
            logger.error(f"下载结果未知错误: {e}")
            raise MineruClientError(f"下载结果失败: {e}") from e

    def parse_pdf(self, pdf_path: Path, output_dir: Path = None) -> tuple[Path, Path]:
        """一站式：提交 -> 轮询 -> 下载，返回 (md_dir, images_dir)
        
        Args:
            pdf_path: PDF 文件路径
            output_dir: 输出目录，默认为 TEMP_OUTPUT_DIR / task_id
            
        Returns:
            (md_dir, images_dir) 元组
        """
        task_id = self.submit_pdf(pdf_path)
        status_result = self.poll_status(task_id)
        
        if status_result.get("status") != "completed":
            raise MineruClientError(f"任务未成功完成: {status_result}")
        
        return self.download_result(task_id, output_dir)

    def read_markdown(self, md_dir: Path) -> str:
        """读取解析结果中最大的 Markdown 文件内容
        
        Args:
            md_dir: Markdown 文件所在目录
            
        Returns:
            Markdown 文件内容
        """
        md_dir = Path(md_dir)
        if not md_dir.exists():
            logger.warning(f"Markdown 目录不存在: {md_dir}")
            return ""
        
        # 查找所有 .md 文件
        md_files = list(md_dir.glob("*.md"))
        
        if not md_files:
            logger.warning(f"目录中未找到 Markdown 文件: {md_dir}")
            return ""
        
        # 选择最大的文件（通常是主文档）
        largest_file = max(md_files, key=lambda f: f.stat().st_size)
        logger.info(f"读取 Markdown 文件: {largest_file.name} ({largest_file.stat().st_size} bytes)")
        
        try:
            return largest_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"读取 Markdown 文件失败: {e}")
            return ""

    def list_images(self, images_dir: Path) -> list[Path]:
        """列出解析结果中的所有图片
        
        Args:
            images_dir: 图片目录
            
        Returns:
            图片文件路径列表
        """
        images_dir = Path(images_dir)
        if not images_dir.exists():
            logger.warning(f"图片目录不存在: {images_dir}")
            return []
        
        # 支持的图片格式
        image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}
        
        images = []
        for ext in image_extensions:
            images.extend(images_dir.glob(f"*{ext}"))
            images.extend(images_dir.glob(f"*{ext.upper()}"))
        
        # 去重并排序
        images = sorted(set(images))
        logger.info(f"找到 {len(images)} 张图片")
        
        return images

    def close(self):
        """关闭 HTTP 客户端"""
        self._client.close()

    def __enter__(self):
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.close()
        return False
