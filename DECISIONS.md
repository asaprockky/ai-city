# Asapville — Decisions Log

Choices made where the brief left room. Newest phase at the bottom.

## Phase 1 — Skeleton + FakeLLM

### Layout & tooling
- **Repo root = project root.** `config.yaml`, `DECISIONS.md`, `alembic.ini` and the `asapville/`
  package live at the top of the repo, not in an extra `asapville/` folder.
- **Python:** code targets 3.12+ (`requires-python = ">=3.12"`). The dev container runs 3.13; nothing
  3.13-only is used.
- **Engine file:** the tick loop lives in `asapville/engine.py` (the brief's layout didn't name a
  file for it). Perception lives in `world/perception.py`, name knowledge in `world/social.py`.
- **Lint:** ruff (E, F, W, I), line length 110.

### Database
- **Money is integer cents** (`money_cents`, 100 cents = 1 asap). Exact on SQL Server and SQLite,
  no float drift, no Decimal-on-SQLite warnings.
- **Portable types:** `Unicode`/`UnicodeText` for human text (NVARCHAR on SQL Server), lengths on
  every indexed string, generic `JSON` (NVARCHAR(max) on SQL Server). No cascading deletes, so no
  SQL Server "multiple cascade paths" errors.
- **`locations.owner_id` is a plain int, not a FK**, to avoid an agents↔locations FK cycle.
- **Alembic owns the schema.** `init-world` runs `alembic upgrade head`. `tests/test_db.py` fails if
  the ORM models drift from the migrations.
- Phase 1 tables: `world_state, locations, agents, events, memories, relationships, llm_calls,
  anomalies, interventions, brands`. Businesses, ledgers, plans, cases, laws come later in new
  migrations.

### LLM layer
- **One gateway.** Every call goes through `LLMGateway.call`: budget check → backend → token & cost
  accounting into `llm_calls`. Rows are buffered and written in the engine's tick transaction (avoids
  SQLite write-lock fights); the budget counters are updated in memory right away.
- **Routing:** `llm/routing.py::model_for(subject)` is the only place that picks a model. Angels use
  an `AngelTask` subject (they have no DB row). The Fixer goes to Opus only at severity ≥4 or after
  2 failed fixes; an Opus call without a `reason` is refused.
- **Effort per tier** (`output_config.effort`): angel `high`, notable `medium`, manager `low`,
  worker = model default. Thinking is left at the model default (adaptive). Turning it off is a 400
  error on these models.
- **Prompt caching:** two cache breakpoints, after the shared world rules and after the persona block.
  The persona block is built in a fixed order and cached per agent so its bytes never change. The
  dynamic part (perception + memories) goes in the user message. Minimum cacheable prefix on the
  5.5 models is 512 tokens, and rules + persona go past that.
- **Pricing** (USD / MTok) is in `config.yaml`: Opus 4/20, Sonnet 2/10, Haiku 0.10/0.50; cache reads
  0.20/0.20/0.01; cache writes at 1.25× input.
- **Budget day = real UTC day.** It is real money, so spend is keyed by the real date, not by
  simulated days. On restart the counters are rebuilt from `llm_calls`.
- **Slow mode** (≥90% of daily cap): ticks get `tick_multiplier`× longer, and Workers only get an
  LLM turn every `worker_decide_every` ticks. On other ticks they keep doing a repeatable action
  (rest/forage/work/idle) or stand idle. **Pause** at 100%: `world_state.paused = true` plus a
  CRITICAL log line and a `system` event. `asapville resume` clears it. An agent that hits its own
  per-agent cap is put on the same "keep going / idle" rule, not paused.
- **FakeLLM** is $0 (`billable=False`) but still estimates tokens and fakes cache hits, so the cost
  panel and caching stats can be tested. With `billable=True` it charges list prices, which tests use
  to drive the budget guard. Its replies are deterministic per (agent, tick).

### Agents
- **Birth:** the world rolls the seed card (≈15% land in selfish/cruel/criminal, with matching dark
  traits and lower agreeableness). The agent's own tier model writes the rest. Big Five, dark traits
  and alignment come from the seed card and the agent cannot change them. Starting money, bread and
  a home come from the wealth bracket (Phase 1: one home plot per citizen, as if born there; choosing
  and renting homes comes with the economy in Phase 4).
- **Identity:** `virtual_mac` is a unique locally-administered unicast MAC (`02:xx:…`).
  `virtual_ip` is unique in `10.42.0.0/16`. They are identity only, with no networking.
- **Names are learned, not known.** Strangers are described by their self-chosen look. Talking face
  to face introduces both people. Overhearing does not. All memory text is written per observer
  ("a stranger (lean, slicked black hair…) said…").
- **Hesitation:** invalid JSON → one retry that quotes the error → still invalid → `idle` +
  `json_parse_failure` anomaly (severity 2). An *invalid action* (valid JSON, impossible move) is
  normal behaviour: it becomes a private `invalid_action` event and a memory ("I tried to … but
  couldn't"), not an anomaly. The Watcher can still scan for these events.

### World rules
- **Time:** tick 0 = day 1, 06:00, spring. 28-day seasons.
- **Sleep is a body rule, not an LLM choice.** Each agent has a bedtime from its seed card (night
  owls / early birds). At bedtime they fall asleep wherever they are (sleeping away from home
  restores less and hurts mood). They wake once the window ends and energy is ≥70. Agents can also
  choose `sleep` (a nap or an early night). People travelling at bedtime finish the walk first.
  Sleeping agents make zero LLM calls. This is the main cost lever.
- **Travel:** Manhattan distance / `cells_per_tick`, rounded up. The hour you set off counts as the
  first hour of walking. Travellers can't act and make no LLM calls. Capacity is checked when you
  arrive: a full building sends you to the Town Square.
- **Simultaneous decisions:** every awake agent decides from the same snapshot of the world. Actions
  then run in a seeded random order and are re-checked at that point (the bread may be gone by then).
- **Needs** (0–100): hunger rises, energy and social fall. Starving (≥80) and famished (100) damage
  health, energy 0 makes you collapse, health 0 kills you. Mood is worked out from the body, and the
  agent's own `mood` word is stored next to it.
- **Foraging** (forest berries, river fish) is the one source of food that costs no money. It costs an
  hour plus energy and can fail. Phase 5 makes it depend on season and weather.
- **Determinism:** the same seed + FakeLLM gives the same world, event for event (tested).
- **Founding brand:** AsapBank is in the registry from day 0. Logo: a plain teal hexagon with one
  white upward chevron (original design).

## Phase 2 — Birth, personality, persona block

- **Founding population = 10** (`world.population`). Tests still use 5 to stay fast.
- **At least one bad apple.** `birth.min_dark` (default 1): if the founding seed cards roll no
  selfish/cruel/criminal citizen, the last neutral card is re-rolled as dark. Later births and
  migrants use the plain ~15% roll.
- **World seed changed 1337 → 2026.** 1337 gave nine "neutral" founders; 2026 gives a mixed town
  without help from `min_dark` (1 criminal, 1 cruel, 3 lawful-good, 5 neutral).
- **The seed card is shown in words as well as numbers** ("agreeableness very low, psychopathy
  high"), so the agent writes a self that fits its traits. The raw 0–1 values are attached too.
- **Names are unique.** The birth prompt lists names already taken. A clash is treated as an
  invalid reply and retried once.
- **Birth gets more room:** `birth.max_tokens` = 4096 and `llm.max_output_tokens` = 2048, because
  adaptive thinking tokens count against `max_tokens`. Only tokens actually used are billed.
- **Credentials are pinned.** The Anthropic backend reads `ASAPVILLE_ANTHROPIC_KEY` explicitly and talks
  to `https://api.anthropic.com`. It ignores any `ANTHROPIC_BASE_URL` or other credentials already
  in the shell, so it can never ride on someone else's credentials. `ASAPVILLE_ANTHROPIC_BASE_URL`
  overrides the URL on purpose. A missing key fails loudly at startup.
- **"Unpleasant" is defined in code** (`persona.unpleasant_reasons`): a dark moral baseline, or any
  dark trait ≥0.6, or agreeableness ≤0.25. `asapville personas --out FILE` writes markdown persona
  cards with the flagged citizens listed at the top.
- **Two ways to supply the key** (`llm.auth`): `env` reads `ASAPVILLE_ANTHROPIC_KEY`. `proxy` is for
  cloud "network secrets", where an egress proxy adds `x-api-key` to requests to
  `api.anthropic.com`. In proxy mode the SDK gets a placeholder key (so it never looks for other
  credentials) and the `X-Api-Key` header is left out, so the proxy's header is the only one sent.
- **`init-world` checks credentials before touching the database**, so a missing key no longer
  leaves a half-made database file.
