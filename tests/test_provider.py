import httpx
import pytest

from app.providers import ChatCompletionsProvider, ModelResponseError, model_error_message


def test_missing_configuration_names_are_reported(monkeypatch):
    monkeypatch.setattr("app.providers.load_dotenv", lambda path: None)
    for name in ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match=r"class1/\.env 缺少配置：LLM_BASE_URL, LLM_API_KEY, LLM_MODEL"):
        ChatCompletionsProvider()


def test_chat_completions_http_contract(monkeypatch):
    monkeypatch.setattr("app.providers.load_dotenv", lambda path: None)
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
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
    assert "enable_thinking" not in received["json"]


@pytest.mark.parametrize("value,expected", [("false", False), ("true", True)])
def test_explicit_thinking_setting_is_sent_and_fingerprinted(monkeypatch, value, expected):
    monkeypatch.setattr("app.providers.load_dotenv", lambda path: None)
    for name, setting in {"LLM_BASE_URL": "https://model.example/v1", "LLM_API_KEY": "test-only", "LLM_MODEL": "test",
                          "LLM_ENABLE_THINKING": value}.items():
        monkeypatch.setenv(name, setting)
    received = {}
    def post(url, **kwargs):
        received.update(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": "{}"}}]})
    monkeypatch.setattr(httpx, "post", post)
    provider = ChatCompletionsProvider()
    assert provider.complete([]) == "{}"
    assert received["enable_thinking"] is expected
    assert provider.evaluation_config()["enable_thinking"] is expected


def test_usage_observation_is_optional_and_config_does_not_expose_credentials(monkeypatch, tmp_path):
    import json
    from app.trace import TurnTrace
    monkeypatch.setattr("app.providers.load_dotenv", lambda path: None)
    monkeypatch.setattr("app.trace.RUNS_DIR", tmp_path)
    monkeypatch.setenv("LLM_BASE_URL", "https://user:password@model.example/v1?key=secret")
    monkeypatch.setenv("LLM_API_KEY", "private-key")
    monkeypatch.setenv("LLM_MODEL", "unit-test")
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(200, request=httpx.Request("POST", url),
        json={"model": "returned-model", "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
              "choices": [{"message": {"content": "{}"}}]}))
    provider = ChatCompletionsProvider()
    assert not any(s in json.dumps(provider.evaluation_config()) for s in ("password", "private-key", "secret"))
    with TurnTrace("session", "data", "question", provider.model) as trace:
        trace.consume("model")
        assert provider.complete([]) == "{}"
    events = [json.loads(line) for line in trace.event_file.read_text(encoding="utf-8").splitlines()]
    response = next(e for e in events if e["event"] == "provider_response")
    assert response["usage"]["total_tokens"] == 10 and response["returned_model"] == "returned-model"


@pytest.mark.parametrize("status", [400, 404])
def test_rejected_model_configuration_reports_http_status(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://model.example"))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    assert f"HTTP {status}" in model_error_message(caught.value)


@pytest.mark.parametrize("content", [b"upstream maintenance", b'{"choices":[]}', b'{"choices":[{"message":{"content":null}}]}'])
def test_invalid_gateway_reply_is_retryable(monkeypatch, content):
    for name, setting in {"LLM_BASE_URL": "https://model.example", "LLM_API_KEY": "example", "LLM_MODEL": "example"}.items():
        monkeypatch.setenv(name, setting)
    request = httpx.Request("POST", "https://model.example")
    replies = iter([httpx.Response(200, request=request, content=content),
                    httpx.Response(200, request=request, json={"choices": [{"message": {"content": "{}"}}]})])
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: next(replies))
    events = []
    monkeypatch.setattr("app.providers.observe", lambda *args: events.append(args))
    provider = ChatCompletionsProvider()
    with pytest.raises(ModelResponseError) as caught:
        provider.complete([])
    assert "无效的对话响应" in model_error_message(caught.value)
    assert provider.complete([]) == "{}"
    assert [e[1] for e in events] == ["invalid_response", "ok"]
