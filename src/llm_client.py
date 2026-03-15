"""Shared LLM API client used by summarizers."""

import time
from typing import Any, Dict, List, Optional

import requests

from config.settings import LLM_CONFIG


class LLMModelClient:
    """LLM API client for generateContent-compatible endpoints."""

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model or LLM_CONFIG["model"]
        self.api_url = f"{LLM_CONFIG['api_url']}/{self.model}:generateContent"
        self.timeout = LLM_CONFIG.get("timeout", 30)

    def _create_request_body(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        prompt = messages[-1]["content"]
        return {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature or LLM_CONFIG["temperature"],
                "maxOutputTokens": max_tokens or LLM_CONFIG["max_output_tokens"],
                "topP": LLM_CONFIG["top_p"],
                "topK": LLM_CONFIG["top_k"],
            },
        }

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        data = self._create_request_body(messages, temperature, max_tokens)

        for attempt in range(LLM_CONFIG["retry_count"]):
            try:
                response = requests.post(
                    f"{self.api_url}?key={self.api_key}",
                    headers=headers,
                    json=data,
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    raise Exception(f"API call failed: {response.text}")
                result = response.json()
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": result["candidates"][0]["content"]["parts"][0]["text"],
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            except requests.Timeout:
                print(f"Request timed out ({self.timeout}s). Retrying...")
                if attempt == LLM_CONFIG["retry_count"] - 1:
                    raise TimeoutError(
                        f"API did not respond within {self.timeout}s after {LLM_CONFIG['retry_count']} retries"
                    )
                time.sleep(LLM_CONFIG["retry_delay"] * (2**attempt))
            except Exception:
                if attempt == LLM_CONFIG["retry_count"] - 1:
                    raise
                time.sleep(LLM_CONFIG["retry_delay"] * (2**attempt))
