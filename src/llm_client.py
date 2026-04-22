"""LLM API 客户端 - 读取 api_config.json 动态选择模型"""
import json
import asyncio
from pathlib import Path
import httpx

from src.config import API_CONFIG_PATH
from src.utils import setup_logging, retry_with_backoff

logger = setup_logging("llm_client")

# 尝试导入 deepseek_v3_tokenizer
try:
    from transformers import AutoTokenizer
    _tokenizer_path = Path(__file__).parent.parent / "deepseek_v3_tokenizer"
    _tokenizer = AutoTokenizer.from_pretrained(str(_tokenizer_path), trust_remote_code=True)
    logger.info("DeepSeek V3 Tokenizer 加载成功")
except Exception as e:
    _tokenizer = None
    logger.warning(f"DeepSeek V3 Tokenizer 加载失败: {e}")


class LLMClient:
    def __init__(self, config_path: Path = None):
        self.config_path = config_path or API_CONFIG_PATH
        self._load_config()
    
    def _load_config(self):
        """加载 api_config.json，根据 active_profile 选择配置"""
        with open(self.config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        self.profile_name = config["active_profile"]
        profile = config["profiles"][self.profile_name]
        self.api_key = profile["API_KEY"]
        self.base_url = profile["BASE_URL"].rstrip("/")
        self.model = profile["MODEL"]
        self.max_concurrent = profile.get("N_PARALLELS", 1)
        # 加载价格信息（向后兼容，可能没有价格字段）
        self.input_price_per_million = profile.get("input_price_per_million")
        self.output_price_per_million = profile.get("output_price_per_million")
        logger.info(f"LLM 配置加载: profile={self.profile_name}, model={self.model}, base_url={self.base_url}")
    
    def estimate_cost(self, system_prompt: str, user_prompt: str, estimated_output_tokens: int = 500) -> dict:
        """使用 deepseek_v3_tokenizer 预估调用成本
        
        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            estimated_output_tokens: 预估输出 token 数，默认 500
            
        Returns:
            {
                "input_tokens": int,
                "estimated_output_tokens": int,
                "input_cost_rmb": float,
                "output_cost_rmb": float,
                "total_cost_rmb": float
            }
            如果无法计算成本（无价格配置或 tokenizer 未加载），返回 None
        """
        # 检查是否有价格配置
        if self.input_price_per_million is None or self.output_price_per_million is None:
            return None
        
        # 计算输入 token 数
        if _tokenizer is not None:
            # 使用 tokenizer 计算输入 token 数
            system_tokens = len(_tokenizer.encode(system_prompt, add_special_tokens=False))
            user_tokens = len(_tokenizer.encode(user_prompt, add_special_tokens=False))
            # 加上消息格式的额外 token（角色标记等，估算约 10 个 token）
            input_tokens = system_tokens + user_tokens + 10
        else:
            # 如果 tokenizer 未加载，使用粗略估算（1 token ≈ 4 字符）
            input_tokens = (len(system_prompt) + len(user_prompt)) // 4
        
        # 计算成本（价格单位：人民币/百万 token）
        input_cost = (input_tokens / 1_000_000) * self.input_price_per_million
        output_cost = (estimated_output_tokens / 1_000_000) * self.output_price_per_million
        total_cost = input_cost + output_cost
        
        return {
            "input_tokens": input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "input_cost_rmb": input_cost,
            "output_cost_rmb": output_cost,
            "total_cost_rmb": total_cost
        }
    
    @retry_with_backoff(max_retries=3, base_delay=2.0)
    def chat_completion(self, system_prompt: str, user_prompt: str, temperature: float = 0.1, max_tokens: int = 2000) -> str:
        """调用 LLM Chat Completion API（OpenAI 兼容格式）
        
        使用 httpx POST 到 {base_url}/chat/completions
        请求体：
        {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        Headers: Authorization: Bearer {api_key}
        
        返回 assistant message content 字符串
        """
        # 预估成本
        cost_estimate = self.estimate_cost(system_prompt, user_prompt, estimated_output_tokens=max_tokens)
        if cost_estimate:
            logger.info(
                f"预估成本: 输入 {cost_estimate['input_tokens']} tokens (¥{cost_estimate['input_cost_rmb']:.4f}), "
                f"输出约 {cost_estimate['estimated_output_tokens']} tokens (¥{cost_estimate['output_cost_rmb']:.4f}), "
                f"合计 ¥{cost_estimate['total_cost_rmb']:.4f}"
            )
        
        url = f"{self.base_url}/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                
                # 提取 assistant 的回复内容
                if "choices" in data and len(data["choices"]) > 0:
                    message = data["choices"][0].get("message", {})
                    content = message.get("content", "")
                    
                    # 记录实际 token 使用和成本
                    usage = data.get("usage", {})
                    if usage and self.input_price_per_million is not None and self.output_price_per_million is not None:
                        actual_input_tokens = usage.get("prompt_tokens", 0)
                        actual_output_tokens = usage.get("completion_tokens", 0)
                        actual_input_cost = (actual_input_tokens / 1_000_000) * self.input_price_per_million
                        actual_output_cost = (actual_output_tokens / 1_000_000) * self.output_price_per_million
                        actual_total_cost = actual_input_cost + actual_output_cost
                        logger.info(
                            f"实际成本: 输入 {actual_input_tokens} tokens (¥{actual_input_cost:.4f}), "
                            f"输出 {actual_output_tokens} tokens (¥{actual_output_cost:.4f}), "
                            f"合计 ¥{actual_total_cost:.4f}"
                        )
                    else:
                        logger.debug(f"LLM 响应成功，token 使用: {usage}")
                    
                    return content.strip()
                else:
                    raise ValueError(f"Unexpected response format: {data}")
                    
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP 错误: {e.response.status_code} - {e.response.text}")
            raise
        except httpx.RequestError as e:
            logger.error(f"请求错误: {e}")
            raise
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise
    
    def chat_completion_json(self, system_prompt: str, user_prompt: str, **kwargs) -> dict:
        """调用 LLM 并解析返回的 JSON
        尝试从返回文本中提取 JSON（处理 markdown code block 包裹的情况）
        
        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            **kwargs: 传递给 chat_completion 的其他参数
            
        Returns:
            解析后的 JSON 字典
            
        Raises:
            ValueError: 如果无法解析 JSON
        """
        # 调用 chat_completion 获取文本响应
        response_text = self.chat_completion(system_prompt, user_prompt, **kwargs)
        
        # 清理返回文本（去除 ```json ... ``` 包裹）
        cleaned_text = response_text
        
        # 处理 markdown code block
        if "```json" in cleaned_text:
            # 提取 ```json 和 ``` 之间的内容
            start = cleaned_text.find("```json") + len("```json")
            end = cleaned_text.find("```", start)
            if end != -1:
                cleaned_text = cleaned_text[start:end].strip()
        elif "```" in cleaned_text:
            # 处理没有 json 标记的 code block
            start = cleaned_text.find("```") + len("```")
            end = cleaned_text.find("```", start)
            if end != -1:
                cleaned_text = cleaned_text[start:end].strip()
        
        # 尝试解析 JSON
        try:
            result = json.loads(cleaned_text)
            return result
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            logger.error(f"原始响应: {response_text[:500]}...")
            raise ValueError(f"无法解析 LLM 返回的 JSON: {e}")
