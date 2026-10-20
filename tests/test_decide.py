import json

import pytest
from sqlalchemy import select

from asapville.agents.decide import parse_decision
from asapville.agents.schemas import ActionName
from asapville.db.models import Anomaly, LLMCall
from asapville.llm.types import ChatMessage, LLMRequest

GOOD = json.dumps({"thought": "hm", "action": "idle", "action_args": {}, "dialogue": None,
                   "mood": "fine"})


def test_parse_plain_and_fenced():
    assert parse_decision(GOOD).action == ActionName.IDLE
    fenced = "Sure!\n```json\n" + GOOD + "\n```"
    assert parse_decision(fenced).mood == "fine"
    assert parse_decision(GOOD.replace('"idle"', '" IDLE "')).action == ActionName.IDLE


@pytest.mark.parametrize("bad", ["", "I walk to the market.", '{"thought": "x"}',
                                 '{"thought":"x","action":"teleport","mood":"a"}', "{not json}"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_decision(bad)


async def test_invalid_json_retries_once_then_hesitates(world, fake, sessions):
    fake.script("decide", *["not json at all"] * 10)  # every decider fails twice
    rep = await world.step()
    assert rep.hesitations == rep.llm_calls // 2 > 0
    with sessions() as s:
        anomalies = list(s.scalars(select(Anomaly).where(Anomaly.source == "engine")))
        assert anomalies and all(a.kind == "json_parse_failure" for a in anomalies)
        retry = [r for r in fake.requests if r.purpose == "decide" and len(r.messages) == 3]
        assert retry and "invalid" in retry[0].messages[-1].content


async def test_retry_success_is_not_an_anomaly(world, fake, sessions):
    fake.script("decide", "garbage", GOOD)
    rep = await world.step()
    assert rep.hesitations == 0
    with sessions() as s:
        assert s.scalars(select(Anomaly).where(Anomaly.source == "engine")).first() is None


async def test_opus_requires_reason(gateway):
    req = LLMRequest(purpose="fixer", model="claude-opus-5-5", tier=0, system_rules="r",
                     messages=[ChatMessage(role="user", content="x")], angel_role="fixer")
    with pytest.raises(ValueError):
        await gateway.call(req, tick=0)
    req.reason = "severity 5 anomaly, sonnet fix failed twice"
    res = await gateway.call(req, tick=0)
    assert res.model == "claude-opus-5-5"
    assert gateway.pending[-1].reason.startswith("severity 5")


async def test_fake_llm_records_tokens_but_costs_nothing(world, sessions):
    await world.step()
    with sessions() as s:
        calls = list(s.scalars(select(LLMCall)))
    assert calls and all(c.cost_usd == 0 for c in calls)
    assert sum(c.input_tokens for c in calls) > 0
    assert any(c.cache_read_tokens > 0 for c in calls)  # repeated persona prefix is "cached"


def test_anthropic_request_shape_caches_static_prefix():
    from asapville.llm.client import AnthropicBackend

    req = LLMRequest(purpose="decide", model="claude-haiku-5-5", tier=3, effort="low",
                     system_rules="RULES", system_persona="PERSONA",
                     messages=[ChatMessage(role="user", content="now")], max_tokens=512,
                     context={"perception": {"secret": "never sent"}})
    params = AnthropicBackend.build_params(req)
    assert [b["text"] for b in params["system"]] == ["RULES", "PERSONA"]
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in params["system"])
    assert params["output_config"] == {"effort": "low"}
    assert params["messages"] == [{"role": "user", "content": "now"}]
    assert "thinking" not in params and "context" not in params and "never sent" not in str(params)
    req.effort = None
    assert "output_config" not in AnthropicBackend.build_params(req)


def test_anthropic_backend_needs_explicit_key(monkeypatch):
    from asapville.llm.client import AnthropicBackend

    monkeypatch.delenv("ASAPVILLE_ANTHROPIC_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ASAPVILLE_ANTHROPIC_KEY"):
        AnthropicBackend()


async def test_anthropic_backend_parses_usage(monkeypatch):
    from types import SimpleNamespace

    from asapville.llm.client import AnthropicBackend

    monkeypatch.setenv("ASAPVILLE_ANTHROPIC_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://host-proxy.invalid")
    backend = AnthropicBackend()
    assert str(backend.client.base_url).startswith("https://api.anthropic.com")
    sent = {}

    async def create(**params):
        sent.update(params)
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=GOOD)],
            usage=SimpleNamespace(input_tokens=11, output_tokens=22, cache_read_input_tokens=300,
                                  cache_creation_input_tokens=0),
            stop_reason="end_turn")

    backend.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    req = LLMRequest(purpose="decide", model="claude-haiku-5-5", tier=3, system_rules="R",
                     messages=[ChatMessage(role="user", content="x")])
    raw = await backend.complete(req)
    assert raw.text == GOOD and raw.cache_read_tokens == 300 and raw.output_tokens == 22
    assert sent["model"] == "claude-haiku-5-5"


async def test_proxy_auth_sends_no_key_header(monkeypatch):
    import anthropic

    from asapville.llm.client import AnthropicBackend

    try:
        import httpx2 as httpx
    except ImportError:  # older SDKs use httpx
        import httpx

    monkeypatch.delenv("ASAPVILLE_ANTHROPIC_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token-must-not-be-used")
    seen = {}

    def handler(request):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": GOOD}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 4}})

    backend = AnthropicBackend(auth="proxy")
    backend.client = backend.client.with_options(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert isinstance(backend.client, anthropic.AsyncAnthropic)
    req = LLMRequest(purpose="decide", model="claude-haiku-5-5", tier=3, system_rules="R",
                     messages=[ChatMessage(role="user", content="x")])
    raw = await backend.complete(req)
    assert raw.text == GOOD and raw.input_tokens == 3
    assert "x-api-key" not in seen and "authorization" not in seen
