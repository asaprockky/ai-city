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

## Phase 7 preview — live dashboard

- **One snapshot row per agent per tick** (`agent_ticks`, migration 0002). The engine writes it at the
  end of every tick, so the dashboard can replay the town hour by hour (positions, needs, mood,
  thought, money, inventory) without re-deriving anything from events. ~10 rows per tick; cheap.
- **`serve` runs the world and the web server in one process**, so the engine's `EventBus` wakes the
  feed the moment a tick lands. `serve --view-only` (or `run` in another terminal) falls back to
  polling the database once a second. Both paths read from the DB, so clients see one update shape.
- **The page is one file, no framework.** `dashboard/static/index.html` renders live (fetch +
  WebSocket, with polling as fallback) and as a replay: `asapville export` embeds the bootstrap JSON
  in the same page. `export_html(fragment=True)` strips the document skeleton for hosts that add
  their own.
- **Observer-only.** Everything the dashboard shows (names, thoughts, secrets) is omniscient; citizens
  never read these tables or endpoints. `system` events are hidden from the feed.
- **Controls write `world_state`** (`paused`, `mode`, `speed`) exactly like the CLI does, so the page,
  `asapville pause` and `asapville speed` stay interchangeable.

## Phase 4 preview — economy, jobs, the town board

- **Why now:** the first real two days starved the town. Everyone walked to the Bakery with money
  in hand and stood in a queue at an unattended counter; one citizen died there. The world had
  shops but no way to trade, and no way to make food. Citizens were behaving sensibly inside
  rules that did not let them live.
- **Money is conserved.** A town fund (`world_state.treasury_cents`, "AsapBank") pays wages and
  takes what shops earn. Citizens → shops → fund → wages → citizens. The fund can run dry.
- **Goods are conserved.** Shelves are `stocks` rows per location. Nothing lands on a shelf unless
  a worker made it: farmer (grain, vegetables → market) → miller (grain → flour) → baker (flour →
  bread). A missing link stops the chain and the worker downstream is not paid for an idle hour.
  Quarryman is a wage-only job. The tavern's stew is the one unlimited good: the safety valve for
  anyone with money, priced high.
- **Shops are unattended.** `buy` works whenever the shelf has stock; a shopkeeper role would have
  reproduced the queue. The market also buys foraged food at 60% of the shelf price.
- **Jobs are chosen (`apply` at the workplace) or given (the Fixer may appoint).** Workers must
  still walk there and choose `work`. Wages, inputs and outputs are per hour in `config.yaml`.
- **The town board.** `write_notice` at the Town Hall or Square; notices stay in everyone's
  perception for a day, signed by name if the reader knows the author. The council (angels) posts
  there too. This is the cheapest way for information to spread without omniscience.
- **Perception tells citizens what they need to plan:** goods and prices where they stand, jobs
  and wages, what each destination sells or hires, their own job. World rules now say how food is
  made, so nobody waits for bread that nobody is baking.

## Phase 6 preview — angels

- **Watcher is code, not an LLM.** It runs every tick for free and files one open anomaly per
  (kind, subject): `starving` (sev 3, 4 when health < 20), `death` (5), `stuck` (3 failures in 6h),
  `no_food_for_sale` (4), `no_workers_<job>` when the product is low (2).
- **Fixer is one LLM call per round**, at most every `angels.fixer_every` ticks and only while
  anomalies are open. Sonnet by default; Opus when severity ≥ 4 or after two failed rounds (via
  `AngelTask`, always with a `reason`). It sees the whole town (omniscient status) and replies with
  JSON interventions: `notice`, `restock`, `appoint`, `grant`, `give_food`. Each one is validated
  and capped (`angels.max_*`), written as an `Intervention` row with before/after, and shown on
  the dashboard's Angels tab. Rejected ones are kept with the reason.
- **Citizens never see angels.** They see a notice from "the town council", a cart unloading at
  the shop, a council letter with a job, a relief parcel. The angel's own summary is a `system`
  event the dashboard shows and no citizen remembers.
- **Lightest touch is a rule in the prompt**, not just a hope: prefer information over hand-outs
  and hand-outs over miracles; never fix what citizens can fix within a few hours.
- **First real round (Opus, tick 48):** it read the situation correctly (food existed, people
  held bread they hadn't eaten, the supply chain had nobody in it), posted a precise notice with
  prices and wages, gave a parcel only to the one citizen at health 8, and appointed a baker and a
  miller. Cost of the round: about three cents.

## Freedom, markets, votes, reciprocity (run-time changes, day 6-10)

Each of these came from watching the real town:

- **Morning plans** (one short call per citizen per waking day, from 05:00) and a "this hour is
  yours" line when the body is fine. In 24 hours nobody had used `improvise`.
- **Angels only inform and save lives** (`angels.allow: [notice, give_food]`). The Fixer may not
  appoint anyone to a job; carts and relief money are off by default.
- **Citizens' stalls** (`open_stall`, `stock_stall`): own name, own price, own till. The first one
  opened on day 6: a citizen buying loaves at the Bakery and reselling them at the tavern.
- **Labour market**: each midnight a job nobody holds, or whose output others idled for (3+ failed
  hours in a day), pays 25% more; a crowded job pays 10% less (0.5x-3x the base wage). Farming paid
  least and nobody farmed until it paid more; a miller who didn't mill got a raise until a
  newcomer took the post.
- **Direct democracy** (`propose_law`, `vote` at the Town Hall or Square): 12-hour votes, majority
  with a quorum of 3; wages and town prices change when a vote passes. Citizens had been writing
  petitions to a council that does not exist.
- **Fullness, not hunger.** Citizens read "hunger 4/100" as an empty stomach; every gauge they see
  now reads higher-is-better.
- **Taste**: the same food three times running costs mood, something new lifts it.
- **Reciprocity**: relationships keep the last six deeds (gifts, purchases, thefts, attacks, and
  the Judge's verdict on improvised acts toward someone), moving trust and liking; witnesses
  remember too, and a public reputation travels. `steal` (often unnoticed, especially from
  sleepers) and `attack` exist. World rules describe town life as a repeated game (tit for tat,
  forgive a single slip, shun repeat betrayers), but each citizen still chooses.
- **Newcomers** (`asapville arrive --preset NAME [--works-for NAME]`): fictional presets only (a
  would-be strongman; an informer with a secret patron). No real people, no real ideologies.
- **Wages follow the chain.** A shortage is blamed on the first link whose own workers were
  working; a crowded link passes it upstream; raw material piling up raises that job's wage; a
  vacant job rises 50% a day. Jobs show who is on the books and who actually turned up.
- **Businesses have owners.** The town sells the farm, mill, bakery, quarry and market
  (`business_prices`; the tavern and clinic stay public as the safety net). An owner's purse is
  their own money: it takes the shop's sales, pays wages and buys raw material from other
  businesses at the seller's price (deliveries are bought by the receiving shop). Owners set
  wages (`hire`), prices (`set_price`) and can `fire`; an owner who can't pay leaves workers
  unpaid. The daily wage adjustment and town votes only touch businesses the town still owns.
- **Lasting memory.** Decisions see the last few hours plus the five most important older
  memories (importance 7+): deals, debts, attacks, purchases.
- **AsapBank** (`deposit`, `withdraw`, `borrow`, `repay` at the bank). The vault is the town
  fund. Savings earn 0.5% a day and can't be stolen; loans cost 2% a day and are due in a week.
  Credit = 20 asaps for anyone with a job + 2x the wages of the last 3 days + half the price of
  businesses owned. Overdue: the bank takes savings, then cash, then forecloses on businesses
  (back to the town); anything still owed is a public default and freezes credit.
- **Food and small businesses.** New ingredients (eggs and milk from a `herder` at the farm, fish
  from a `fisher` at the river, flour and grain on sale) and nine dishes (`recipes`: soup,
  omelette, porridge, pancakes, fish pie, grilled fish, berry tart, cheese, fish hotpot). Anyone
  can `cook` at home or in a shop or business they own; a good dish lifts the mood (`food.mood`).
  Cook, open a stall, set a price: that is a restaurant. No recipe is forced on anyone.
- **Against routine.** Morning plans use the stronger model, see yesterday's plan and the last
  two days' waking hours, and must include one step that is new or toward an ambition. Two days
  of nothing but work, food and walking is noticed in perception and costs mood each day.
- **No demand, no wage; taxes.** A shelf holding `shelf_cap` (40) of an item takes no more, so a
  job whose outputs all pile up unsold pays nothing until people buy. The town takes 5% of every
  wage and 10% of every sale by a citizen's business or stall; it pays the wages of the
  businesses it still owns (farm, mill, quarry, the herder's and fisher's work).
- **The town council.** An election opens whenever no council sits: for 24 hours anyone may
  `stand` (with a pitch, at the Town Hall or Square) and everyone may `elect` one candidate from
  anywhere; the top three sit for a week. While they sit, proposals are theirs to decide (two
  yes pass, two no sink, at once), and a proposal may now be a `grant` from the town fund to a
  citizen. Votes on proposals can be cast from anywhere too (turnout had sunk good proposals).
- **Problem solving.** World rules ask each citizen to name their most pressing problem, weigh
  two or three ways to solve it and never repeat what just failed; perception lists the last
  three setbacks with their reasons, and morning plans start from the biggest problem or goal.
- **Manga** (`asapville comic --hours 24 [--post]`). One call to the stronger model writes a page
  (title, 4-6 panels, captions, bubbles, sound effects) from the real events; code draws it in
  black ink and screentone with a fixed face per citizen (hair tone from their own words, style
  from their name, a sly brow for the scheming), and headless Chromium makes the PNG. `--post`
  sends it to Telegram (`ASAPVILLE_TELEGRAM_TOKEN`, `ASAPVILLE_TELEGRAM_CHAT`). Chapters go
  to `manga/`.
  Portraits: `asapville portraits` asks Gemini (`ASAPVILLE_GEMINI_KEY`) for a manga portrait of
  every citizen without one, from their own appearance, into `manga/faces/<id>.png`; `comic`
  draws newcomers' portraits first. Five portraits so far came from Canva.
  Panel art: the script gives every panel a `scene` for the illustrator; with an image service
  key in the environment (Cloudflare Workers AI `CLOUDFLARE_ACCOUNT_ID`+`CLOUDFLARE_API_TOKEN`,
  `FAL_KEY` or `OPENAI_API_KEY`) each panel is drawn from the scene plus every character's fixed
  look (from their card), turned to greyscale, and used as the panel's background under the
  code-set captions and bubbles. Without a key, panels show the portraits.
  Cloudflare model: `ASAPVILLE_CF_IMAGE_MODEL`, default `@cf/black-forest-labs/flux-2-dev`, the
  catalogue's text-to-image model that takes reference pictures (FLUX.2: up to four
  `input_image_N`, multipart form, under 512x512). Each panel sends its cast's portraits from
  `manga/faces` as references, named in the prompt (image 0 is ...), so faces hold from panel to
  panel; FLUX.1 schnell kept no faces. Other models (FLUX.2 klein, Leonardo Lucid Origin and
  Phoenix, SDXL) work by name, each with its own request shape, and go without references.
  Panels are drawn in the page's shapes (square, or wide for an odd last panel).

## The Asapville Gazette and breaking news (Telegram)
A bare list of where people stand was dull. The channel now gets a paper: one Sonnet call reads
the state of the town and the hours' events with the citizens' own words, and writes a headline,
three to five stories with real quotes, power, food and work, a line on everyone, and what to watch.
The same JSON becomes the channel post (`as_text`) and a self-contained newspaper page with faces,
a map of who is where, money/fed/health bars and the shelves (`render_html`), sent as a file.
`asapville news` posts a short flash for each big moment (a death, an election result, a business
bought, a default, a law, a secret deal, a candidacy) as soon as a chunk ends, whatever its length;
a mark file remembers the last event posted. Nothing may be invented: only what is in the material.
`asapville chunk` posts the bulletin the owner liked best after every 12 hours: "Часы X–Y", then
6–9 bullets with real quotes and exact numbers, and an optional builder's note (what was fixed).
Daily budget raised to $15 at the owner's request.

## Who is who (`asapville cast`)
The owner kept forgetting who is who. `asapville cast --post` sends a page with a table of every
citizen (portrait, Russian name, who they are in a line, job, council seat, what they own, home,
money with savings and debt) and a card each: character, what they want, their secret (readers only),
how they talk, how to recognise them, whom they like and dislike, debts. The lasting part is written
once from each personality card and kept in `saves/cast.json`; the rest is read fresh. The page is
pinned in the channel; a refresh pins the new one and unpins the old (`saves/cast-pin.txt`).
Plain bank deposits and withdrawals no longer count as breaking news.

## Improvising never pays anyone
Abner "left 3.50 under a cup" for Ruth: the Judge narrated it, his memory said "paid in full", but
improvising moves nothing, so the coins stayed in his purse and Ruth found only a wet ring. He then
paid her twice with `give`. Now the Judge is told that coins left, slipped or "paid" by improvising
stay with the citizen, and in code any improvised attempt that mentions money adds to the memory
that the money never left the purse and must be given in person.
Even with that, Abner paid Ruth the same 3.50 four times (hours 994, 1017, 1022, 1033): each `give`
left only a small memory, and his morning plan kept saying "pay Ruth". Now every citizen sees the
money they handed to each person in the last 72 hours ("Ruth Caddick 14.00 asaps, 4 times, last 2
hours ago") with a reminder that a debt paid is settled.

## Feelings
The owner asked for feelings like people have. Each citizen now carries up to eight feelings
(`Agent.feelings`, migration 0011): an emotion from a fixed list of 25 (joy, gratitude, love, pride,
hope, anger, resentment, fear, grief, shame, guilt, jealousy, envy, loneliness...), a strength 0-100,
often the person it is about, and why. They come from two places. Events (`feelings.appraise`, run
for every emitted event): a gift brings gratitude toward the giver, a caught theft anger and fear, a
blow anger and fear, a death grief to everyone who cared and a little fear to all, being fired anger
and shame, an election pride or disappointment, a default shame; and the body each hour: starving
brings fear, long solitude loneliness. And the citizen themselves: each decision may name up to
three feelings in character, which is where envy, love, guilt and hope mostly come from (capped at
90 so one hour never starts at full strength). Each emotion fades at its own pace (irritation in
hours, grief and love over weeks) and while it lasts pulls the mood up or down. Citizens see their
feelings ("envy toward Corwin (strong): he owns the bakery") with the reminder that real feelings
colour what one says and does; they may act on them, hide them or fight them. The reports and the
who-is-who page show them too.
