import httpx
import pytest

from app.providers import ChatCompletionsProvider


def test_missing_configuration_names_are_reported(monkeypatch):
    monkeypatch.setattr("app.providers.load_dotenv", lambda path: None)
    for name in ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match=r"class1/\.env 缺少配置：LLM_BASE_URL, LLM_API_KEY, LLM_MODEL"):
        ChatCompletionsProvider()


def test_chat_completions_http_contract(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "unit-test-key")
    monkeypatch.setenv("LLM_MODEL", "unit-test-model")
    received = {}

    def post(url, **kwargs):
        received.update(url=url, **kwargs)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"intent":"clarify"}'}}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    provider = ChatCompletionsProvider()
    assert provider.complete([{"role": "user", "content": "test"}]) == '{"intent":"clarify"}'
    assert received["url"] == "https://model.example/v1/chat/completions"
    assert received["json"]["response_format"] == {"type": "json_object"}
    assert received["json"]["model"] == "unit-test-model"
