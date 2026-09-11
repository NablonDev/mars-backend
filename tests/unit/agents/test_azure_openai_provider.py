"""Tests for AzureOpenAIChatClient. `ChatOpenAI` is mocked -- never a live call."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, SecretStr

from app.agents.providers.azure_openai import (
    AzureOpenAIChatClient,
    AzureOpenAIConfigError,
)
from app.core.config import LLMConfig


class _Extraction(BaseModel):
    value: str = ""


def _configured_config(**overrides) -> LLMConfig:
    defaults = {
        "api_key": "fake-key",
        "endpoint": "https://example.openai.azure.com/openai/v1",
        "deployment": "fake-deployment",
    }
    defaults.update(overrides)
    return LLMConfig(**defaults)


def test_raises_clear_error_if_azure_openai_not_configured():
    config = LLMConfig(api_key="", endpoint="", deployment="")
    with pytest.raises(AzureOpenAIConfigError):
        AzureOpenAIChatClient(config)


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_constructor_passes_config_through_to_chat_openai(mock_chat_openai: MagicMock):
    AzureOpenAIChatClient(_configured_config(), timeout_seconds=42.0, max_retries=5)

    mock_chat_openai.assert_called_once_with(
        base_url="https://example.openai.azure.com/openai/v1",
        api_key=SecretStr("fake-key"),
        model="fake-deployment",
        timeout=42.0,
        max_retries=5,
    )


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_constructor_defaults_match_module_constants(mock_chat_openai: MagicMock):
    client = AzureOpenAIChatClient(_configured_config())

    assert client.model_name == "fake-deployment"
    mock_chat_openai.assert_called_once_with(
        base_url="https://example.openai.azure.com/openai/v1",
        api_key=SecretStr("fake-key"),
        model="fake-deployment",
        timeout=90.0,
        max_retries=3,
    )


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_invoke_without_tools_skips_bind_tools(mock_chat_openai: MagicMock):
    mock_llm_instance = MagicMock()
    mock_chat_openai.return_value = mock_llm_instance
    mock_llm_instance.invoke.return_value = "response"

    client = AzureOpenAIChatClient(_configured_config())
    result = client.invoke([])

    mock_llm_instance.bind_tools.assert_not_called()
    mock_llm_instance.invoke.assert_called_once_with([])
    assert result == "response"


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_invoke_with_tools_binds_tools_before_invoking(mock_chat_openai: MagicMock):
    mock_llm_instance = MagicMock()
    mock_bound = MagicMock()
    mock_llm_instance.bind_tools.return_value = mock_bound
    mock_chat_openai.return_value = mock_llm_instance
    mock_bound.invoke.return_value = "response"

    tools = [MagicMock()]
    client = AzureOpenAIChatClient(_configured_config())
    result = client.invoke([], tools=tools)

    mock_llm_instance.bind_tools.assert_called_once_with(tools)
    mock_bound.invoke.assert_called_once_with([])
    assert result == "response"


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_invoke_structured_without_overrides_skips_bind(mock_chat_openai: MagicMock):
    mock_llm_instance = MagicMock()
    mock_structured = MagicMock()
    mock_chat_openai.return_value = mock_llm_instance
    mock_llm_instance.with_structured_output.return_value = mock_structured
    mock_structured.invoke.return_value = _Extraction(value="ok")

    client = AzureOpenAIChatClient(_configured_config())
    result = client.invoke_structured([], _Extraction)

    mock_llm_instance.bind.assert_not_called()
    mock_llm_instance.with_structured_output.assert_called_once_with(_Extraction)
    mock_structured.invoke.assert_called_once_with([])
    assert result == _Extraction(value="ok")


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_invoke_with_model_override_binds_model_only_for_that_call(mock_chat_openai: MagicMock):
    mock_llm_instance = MagicMock()
    mock_bound = MagicMock()
    mock_llm_instance.bind.return_value = mock_bound
    mock_chat_openai.return_value = mock_llm_instance
    mock_bound.invoke.return_value = "response"

    client = AzureOpenAIChatClient(_configured_config())
    result = client.invoke([], model="gpt-5-mini")

    mock_llm_instance.bind.assert_called_once_with(model="gpt-5-mini")
    mock_bound.invoke.assert_called_once_with([])
    assert result == "response"


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_invoke_merges_tools_model_and_kwargs_into_one_bind_tools_call(mock_chat_openai: MagicMock):
    mock_llm_instance = MagicMock()
    mock_bound = MagicMock()
    mock_llm_instance.bind_tools.return_value = mock_bound
    mock_chat_openai.return_value = mock_llm_instance
    mock_bound.invoke.return_value = "response"

    tools = [MagicMock()]
    client = AzureOpenAIChatClient(_configured_config())
    result = client.invoke([], tools=tools, model="gpt-5-mini", top_p=0.9)

    mock_llm_instance.bind_tools.assert_called_once_with(tools, model="gpt-5-mini", top_p=0.9)
    mock_bound.invoke.assert_called_once_with([])
    assert result == "response"


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_invoke_structured_wraps_a_dict_result_into_the_model(mock_chat_openai: MagicMock):
    mock_llm_instance = MagicMock()
    mock_structured = MagicMock()
    mock_chat_openai.return_value = mock_llm_instance
    mock_llm_instance.with_structured_output.return_value = mock_structured
    mock_structured.invoke.return_value = {"value": "from-dict"}

    client = AzureOpenAIChatClient(_configured_config())
    result = client.invoke_structured([], _Extraction)

    assert result == _Extraction(value="from-dict")


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_invoke_structured_with_tools_forces_json_schema_method(mock_chat_openai: MagicMock):
    mock_llm_instance = MagicMock()
    mock_structured = MagicMock()
    mock_chat_openai.return_value = mock_llm_instance
    mock_llm_instance.with_structured_output.return_value = mock_structured
    mock_structured.invoke.return_value = _Extraction(value="tooled")

    tools = [MagicMock()]
    client = AzureOpenAIChatClient(_configured_config())
    result = client.invoke_structured([], _Extraction, tools=tools)

    mock_llm_instance.with_structured_output.assert_called_once_with(
        _Extraction, method="json_schema", tools=tools
    )
    assert result == _Extraction(value="tooled")


@patch("app.agents.providers.azure_openai.ChatOpenAI")
def test_invoke_structured_with_tools_respects_an_explicit_method_override(mock_chat_openai: MagicMock):
    mock_llm_instance = MagicMock()
    mock_structured = MagicMock()
    mock_chat_openai.return_value = mock_llm_instance
    mock_llm_instance.with_structured_output.return_value = mock_structured
    mock_structured.invoke.return_value = _Extraction(value="tooled")

    tools = [MagicMock()]
    client = AzureOpenAIChatClient(_configured_config())
    client.invoke_structured([], _Extraction, tools=tools, method="function_calling")

    mock_llm_instance.with_structured_output.assert_called_once_with(
        _Extraction, method="function_calling", tools=tools
    )
