"""Thin adapter over the Google Gen AI SDK.

The agent works with small neutral types (Turn / ToolCall / LLMResponse) so it can be tested with a
scripted fake client and so the LLM provider stays swappable. Gemini's raw model Content is kept on
model turns: thinking models require their thought signatures to be echoed back during tool use.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Settings, get_settings

log = logging.getLogger("omniagent.llm")


class LLMError(Exception):
    """Safe-to-display failure of the AI service."""


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class Turn:
    role: str  # user | model | tool
    text: str | None = None
    calls: list[ToolCall] = field(default_factory=list)  # model turn requesting tools
    results: list[tuple[ToolCall, dict]] = field(default_factory=list)  # tool turn
    raw: Any = None  # provider-native content (model turns)


@dataclass
class LLMResponse:
    text: str | None
    calls: list[ToolCall]
    raw: Any = None


class LLMClient(Protocol):
    def generate(self, system: str, turns: list[Turn], tools: list[dict], timeout: float) -> LLMResponse: ...

    def embed(self, texts: list[str], task: str) -> list[list[float]]: ...


class GeminiClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        key = self.settings.gemini_api_key.get_secret_value()
        if not key:
            raise LLMError("The Gemini API key is not configured on the server (set GEMINI_API_KEY).")
        from google import genai  # imported lazily so tests run without the SDK configured

        self._genai = genai
        self._client = genai.Client(api_key=key)

    # ── generation ──
    def _contents(self, turns: list[Turn]):
        from google.genai import types

        out = []
        for t in turns:
            if t.role == "user":
                out.append(types.Content(role="user", parts=[types.Part.from_text(text=t.text or "")]))
            elif t.role == "model":
                if t.raw is not None:
                    out.append(t.raw)
                else:
                    parts = []
                    if t.text:
                        parts.append(types.Part.from_text(text=t.text))
                    for c in t.calls:
                        parts.append(types.Part.from_function_call(name=c.name, args=c.args))
                    out.append(types.Content(role="model", parts=parts))
            else:  # tool results
                out.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_function_response(name=c.name, response=r) for c, r in t.results],
                    )
                )
        return out

    def generate(self, system: str, turns: list[Turn], tools: list[dict], timeout: float) -> LLMResponse:
        from google.genai import types

        decls = [
            types.FunctionDeclaration(
                name=t["name"], description=t["description"], parameters=types.Schema.model_validate(t["parameters"])
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
            http_options=types.HttpOptions(timeout=int(timeout * 1000)),
        )
        try:
            resp = self._client.models.generate_content(
                model=self.settings.gemini_model, contents=self._contents(turns), config=config
            )
        except Exception as e:  # SDK raises several error types; never leak details to the browser
            log.error("Gemini request failed: %s", type(e).__name__)
            msg = str(e).lower()
            if "api key" in msg or "permission" in msg or "unauth" in msg:
                raise LLMError("The Gemini API key was rejected. Check GEMINI_API_KEY on the server.") from None
            if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                raise LLMError("The Gemini API quota was exceeded. Try again shortly.") from None
            raise LLMError("The AI service is temporarily unavailable. Please try again.") from None

        if not resp.candidates or not resp.candidates[0].content:
            raise LLMError("The AI service returned an empty response. Please rephrase and try again.")
        content = resp.candidates[0].content
        calls, texts = [], []
        for p in content.parts or []:
            if getattr(p, "function_call", None):
                fc = p.function_call
                calls.append(ToolCall(fc.name, dict(fc.args or {}), getattr(fc, "id", None)))
            elif getattr(p, "text", None) and not getattr(p, "thought", False):
                texts.append(p.text)
        return LLMResponse("".join(texts).strip() or None, calls, content)

    # ── embeddings ──
    def embed(self, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        from google.genai import types

        vectors: list[list[float]] = []
        try:
            for i in range(0, len(texts), 50):
                res = self._client.models.embed_content(
                    model=self.settings.embedding_model,
                    contents=texts[i : i + 50],
                    config=types.EmbedContentConfig(
                        task_type=task, output_dimensionality=self.settings.embedding_dimensions
                    ),
                )
                vectors.extend(list(e.values) for e in res.embeddings)
        except Exception as e:
            log.error("Gemini embedding failed: %s", type(e).__name__)
            raise LLMError("The embedding service is unavailable. Please try again.") from None
        return vectors


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = GeminiClient()
    return _client


def set_llm(client: LLMClient | None) -> None:  # tests / alternative providers
    global _client
    _client = client
