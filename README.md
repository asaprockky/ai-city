# Asapville

A small-town simulation where every citizen is an LLM-driven person with a fixed personality,
real needs, and real consequences. See `DECISIONS.md` for design choices.

## Quick start (free, uses FakeLLM)

```bash
pip install -e ".[dev]"
asapville init-world            # creates asapville.db (SQLite), the map and 5 citizens
asapville run --days 2 --fast   # simulate 48 hours without waiting between ticks
asapville report                # world state, last events, cost summary
asapville personas              # every citizen's persona card
pytest                          # full test suite, $0
```

Other commands: `asapville pause`, `asapville resume`, `asapville speed 2.0`.

## Watch the town live

```bash
asapville serve                 # runs the world AND a live dashboard at http://127.0.0.1:8420
asapville serve --days 2 --fast # run two days as fast as the LLM allows, keep serving afterwards
asapville serve --view-only     # just show the database (live if `asapville run` is on elsewhere)
asapville export --out town.html   # one self-contained replay page, no server needed
```

The dashboard shows the map with every citizen (walking, sleeping, talking), a scrubbable
timeline of every hour lived, each citizen's needs, mood, thoughts and persona, the event feed
and the LLM cost ledger. Pause, resume and speed work from the page.

## Real Claude models

```bash
export ASAPVILLE_ANTHROPIC_KEY=...         # never put it in config.yaml
export ASAPVILLE_LLM_BACKEND=anthropic
```

If the key is stored as a cloud **network secret** (the proxy adds the `x-api-key` header, so the
key never enters the container), use proxy mode instead of the env var:

```bash
export ASAPVILLE_LLM_AUTH=proxy      # secret: header x-api-key, no prefix, host api.anthropic.com
```

Phase 2 demo (10 real births, then the persona cards):

```bash
pip install -e .                # once per fresh session; provides the `asapville` command
rm -f asapville.db
ASAPVILLE_LLM_BACKEND=anthropic asapville init-world
asapville personas --out reports/persona_cards.md
asapville report --events 0     # cost summary of the births
```

Budget caps (`budget:` in `config.yaml`) are hard limits: slow mode at 90% of the daily cap,
pause at 100%.

## SQL Server

```bash
pip install -e ".[mssql]"
export DB_URL="mssql+pyodbc://user:pass@host/asapville?driver=ODBC+Driver+18+for+SQL+Server"
asapville init-world
```

Schema changes go through Alembic (`alembic revision --autogenerate -m "..."`).
