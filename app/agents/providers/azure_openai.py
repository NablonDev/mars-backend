"""Azure OpenAI chat client.

This is the only place a `ChatOpenAI` client is constructed; every other module
that needs one goes through `AzureOpenAIChatClient`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

from app.core.config import LLMConfig

TStructuredModel = TypeVar("TStructuredModel", bound=BaseModel)


class AzureOpenAIConfigError(RuntimeError):
    """Raised when Azure OpenAI configuration is missing."""


class AzureOpenAIChatClient:
    """Chat client for Azure OpenAI through LangChain."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        if not (config.api_key and config.endpoint and config.deployment):
            raise AzureOpenAIConfigError(
                "Azure OpenAI is not configured. Set AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, "
                "and AZURE_OPENAI_DEPLOYMENT_NAME."
            )

        if timeout_seconds is None:
            timeout_seconds = config.timeout_seconds
        if max_retries is None:
            max_retries = config.max_retries

        self._model_name = config.deployment
        self._llm = ChatOpenAI(
            base_url=config.endpoint,
            api_key=SecretStr(config.api_key),
            model=config.deployment,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    @property
    def model_name(self) -> str:
        """The configured Azure OpenAI deployment name, recorded alongside generated summaries."""
        return self._model_name

    def invoke(
        self,
        messages: Sequence[BaseMessage],
        *,
        tools: Sequence[BaseTool] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        """Send messages to the model and return its response.

        `tools`, `model`, and `kwargs` all bind for this call only, so one client
        serves both the tool-calling rounds and the final tool-free call of an
        agent loop, and a caller can override the deployment for one call without
        affecting any other call made through this client.
        """
        overrides: dict[str, Any] = dict(kwargs)
        if model is not None:
            overrides["model"] = model

        llm: Runnable[LanguageModelInput, AIMessage]
        if tools:
            llm = self._llm.bind_tools(tools, **overrides)
        elif overrides:
            llm = self._llm.bind(**overrides)
        else:
            llm = self._llm

        return llm.invoke(list(messages))

    def invoke_structured(
        self,
        messages: Sequence[BaseMessage],
        output_schema: type[TStructuredModel],
        *,
        tools: Sequence[BaseTool] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> TStructuredModel:
        """Send messages to the model and parse the response into `output_schema`.

        `tools`, `model`, and `kwargs` bind for this call only. Passing `tools`
        forces `method="json_schema"` (unless `kwargs` already sets `method`),
        since `ChatOpenAI.with_structured_output` only wires bound tools through
        in that mode, dropping them silently in the default `function_calling` mode.
        """
        structured_kwargs: dict[str, Any] = dict(kwargs)
        if model is not None:
            structured_kwargs["model"] = model

        if tools:
            structured_kwargs.setdefault("method", "json_schema")
            structured_kwargs["tools"] = tools

        structured = self._llm.with_structured_output(output_schema, **structured_kwargs)
        result = structured.invoke(list(messages))
        if isinstance(result, output_schema):
            return result

        assert isinstance(result, dict)
        return output_schema(**result)
