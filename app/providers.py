import os
from typing import Protocol

import httpx
from dotenv import load_dotenv

from app.config import ROOT


class Provider(Protocol):
    model: str

    def complete(self, messages: list[dict]) -> str: ...


class ChatCompletionsProvider:
    """显式配置的 Chat Completions JSON 协议；没有默认模型和协议探测。"""

    def __init__(self):
        load_dotenv(ROOT / ".env")
        missing = [name for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not os.getenv(name)]
        if missing:
            raise ValueError(f"class1/.env 缺少配置：{', '.join(missing)}；请从 .env.example 复制并填写")
        self.base_url = os.environ["LLM_BASE_URL"].rstrip("/")
        self.api_key = os.environ["LLM_API_KEY"]
        self.model = os.environ["LLM_MODEL"]
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))

    def complete(self, messages: list[dict]) -> str:
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "temperature": 0, "response_format": {"type": "json_object"}},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
