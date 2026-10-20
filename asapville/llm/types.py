"""Request/response types shared by every LLM backend."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    purpose: str  # birth | decide | importance | reflect | plan | watcher | fixer | judge ...
    model: str
    tier: int  # 0 angel, 1 notable, 2 manager, 3 worker
    effort: str | None = None
    # Static prefix, cached: world rules first (shared by everyone), persona second.
    system_rules: str
    system_persona: str | None = None
    messages: list[ChatMessage]
    max_tokens: int = 1024
    json_schema: dict[str, Any] | None = None  # optional structured-output constraint
    agent_id: int | None = None
    angel_role: str | None = None
    reason: str | None = None  # mandatory for Opus calls
    # Structured view of the prompt for FakeLLM only. Never sent to a real API.
    context: dict[str, Any] = Field(default_factory=dict)


class RawCompletion(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str | None = None


class LLMResult(RawCompletion):
    model: str
    cost_usd: float = 0.0
    latency_ms: int = 0


class LLMBackend(Protocol):
    name: str
    billable: bool  # False for FakeLLM: tokens are tracked but cost is $0

    async def complete(self, req: LLMRequest) -> RawCompletion: ...
