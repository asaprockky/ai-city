"""LLM gateway: budget check -> backend call -> cost & token accounting.

Every LLM call in the simulation goes through `LLMGateway.call`. The Anthropic
backend reads ASAPVILLE_ANTHROPIC_KEY from the environment;
the key is never logged or stored.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from sqlalchemy.orm import Session

from asapville.config import LLMCfg, Pricing
from asapville.db.models import LLMCall
from asapville.llm.budget import BudgetGuard
from asapville.llm.types import LLMBackend, LLMRequest, LLMResult, RawCompletion


def cost_usd(raw: RawCompletion, price: Pricing | None) -> float:
    if price is None:
        return 0.0
    return (raw.input_tokens * price.input + raw.output_tokens * price.output
            + raw.cache_read_tokens * price.cache_read
            + raw.cache_write_tokens * price.cache_write) / 1_000_000


class AnthropicBackend:
    """Real Claude calls via the official SDK, with prompt caching on the static prefix."""

    name = "anthropic"
    billable = True

    def __init__(self, auth: str = "env") -> None:
        import anthropic  # local import: tests never need it

        # Explicit key + base URL: never fall back to other ambient credentials or a host-set
        # ANTHROPIC_BASE_URL. ASAPVILLE_ANTHROPIC_BASE_URL overrides the URL on purpose.
        base_url = os.environ.get("ASAPVILLE_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        if auth == "proxy":
            # The key lives outside this process; the egress proxy adds x-api-key. The
            # placeholder only stops the SDK from looking for other credentials, and the
            # header itself is omitted so the proxy's value is the only one sent.
            self.client = anthropic.AsyncAnthropic(
                api_key="injected-by-proxy", base_url=base_url,
                default_headers={"X-Api-Key": anthropic.omit})
            return
        api_key = os.environ.get("ASAPVILLE_ANTHROPIC_KEY")
        if not api_key:
            raise RuntimeError(
                "ASAPVILLE_ANTHROPIC_KEY is not set; the anthropic backend needs it "
                "(or set llm.auth=proxy / ASAPVILLE_LLM_AUTH=proxy if a proxy adds the key)")
        self.client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)

    @staticmethod
    def build_params(req: LLMRequest) -> dict[str, Any]:
        # Two cache breakpoints: after the shared world rules, and after the
        # persona block, so rules are reused across agents and rules+persona per agent.
        system: list[dict[str, Any]] = [
            {"type": "text", "text": req.system_rules, "cache_control": {"type": "ephemeral"}}
        ]
        if req.system_persona:
            system.append({"type": "text", "text": req.system_persona,
                           "cache_control": {"type": "ephemeral"}})
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "system": system,
            "messages": [m.model_dump() for m in req.messages],
        }
        output_config: dict[str, Any] = {}
        if req.effort:
            output_config["effort"] = req.effort
        if req.json_schema:
            output_config["format"] = {"type": "json_schema", "schema": req.json_schema}
        if output_config:
            params["output_config"] = output_config
        return params

    async def complete(self, req: LLMRequest) -> RawCompletion:
        resp = await self.client.messages.create(**self.build_params(req))
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        return RawCompletion(
            text=text,
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            stop_reason=resp.stop_reason,
        )


class LLMGateway:
    def __init__(self, backend: LLMBackend, cfg: LLMCfg, guard: BudgetGuard) -> None:
        self.backend = backend
        self.cfg = cfg
        self.guard = guard
        self.semaphore = asyncio.Semaphore(cfg.max_concurrency)
        self.pending: list[LLMCall] = []  # flushed into the engine's session each tick

    async def call(self, req: LLMRequest, *, tick: int) -> LLMResult:
        if req.model == self.cfg.models.opus and not req.reason:
            raise ValueError("every Opus call must carry a reason")
        self.guard.check(agent_id=req.agent_id, model=req.model, angel_role=req.angel_role)
        started = time.perf_counter()
        error: str | None = None
        raw = RawCompletion(text="")
        try:
            async with self.semaphore:
                raw = await self.backend.complete(req)
            return self._result(req, raw, started)
        except Exception as exc:  # recorded, then re-raised
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._account(req, raw, started, tick, error)

    def _price(self, model: str) -> Pricing | None:
        return self.cfg.pricing.get(model) if self.backend.billable else None

    def _result(self, req: LLMRequest, raw: RawCompletion, started: float) -> LLMResult:
        return LLMResult(**raw.model_dump(), model=req.model,
                         cost_usd=cost_usd(raw, self._price(req.model)),
                         latency_ms=int((time.perf_counter() - started) * 1000))

    def _account(self, req: LLMRequest, raw: RawCompletion, started: float, tick: int,
                 error: str | None) -> None:
        cost = cost_usd(raw, self._price(req.model))
        self.guard.record(cost, agent_id=req.agent_id, model=req.model, angel_role=req.angel_role)
        self.pending.append(LLMCall(
            real_day=self.guard.day, tick=tick, agent_id=req.agent_id,
            angel_role=req.angel_role, purpose=req.purpose, backend=self.backend.name,
            model=req.model, tier=req.tier, effort=req.effort,
            input_tokens=raw.input_tokens, output_tokens=raw.output_tokens,
            cache_read_tokens=raw.cache_read_tokens, cache_write_tokens=raw.cache_write_tokens,
            cost_usd=cost, latency_ms=int((time.perf_counter() - started) * 1000),
            ok=error is None, error=error, reason=req.reason,
        ))

    def flush(self, session: Session) -> int:
        n = len(self.pending)
        session.add_all(self.pending)
        self.pending = []
        return n


def make_backend(cfg: LLMCfg) -> LLMBackend:
    if cfg.backend == "anthropic":
        return AnthropicBackend(auth=cfg.auth)
    from asapville.llm.fake import FakeLLM

    return FakeLLM()
