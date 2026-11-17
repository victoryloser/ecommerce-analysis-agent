import os
import time
import hashlib
from urllib.parse import urlsplit
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
        thinking = os.getenv("LLM_ENABLE_THINKING")
        if thinking is not None and thinking.lower() not in {"true", "false"}:
            raise ValueError("LLM_ENABLE_THINKING 必须为 true 或 false；不配置则使用服务默认值")
        self.enable_thinking = None if thinking is None else thinking.lower() == "true"

    def complete(self, messages: list[dict]) -> str:
        started = time.perf_counter()
        payload = {"model": self.model, "messages": messages, "temperature": 0, "response_format": {"type": "json_object"}}
        if self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        from app.trace import CURRENT_TRACE
        trace = CURRENT_TRACE.get()
        if trace:
            trace.record("provider_response", configured_model=self.model, returned_model=body.get("model"),
                         usage={k: v for k, v in body.get("usage", {}).items()
                                if k in {"prompt_tokens", "completion_tokens", "total_tokens"}} if body.get("usage") else None,
                         elapsed_ms=round((time.perf_counter()-started)*1000))
        return body["choices"][0]["message"]["content"]

    def evaluation_config(self):
        endpoint = urlsplit(self.base_url)
        return {"model": self.model, "temperature": 0, "protocol": "chat-completions-json",
                "enable_thinking": self.enable_thinking,
                "timeout_seconds": self.timeout, "retries": 0,
                "service_sha256": hashlib.sha256(f"{endpoint.hostname}{endpoint.path}".encode()).hexdigest()}
