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

## Real Claude models

```bash
export ANTHROPIC_API_KEY=...         # never put it in config.yaml
export ASAPVILLE_LLM_BACKEND=anthropic
```

Phase 2 demo (10 real births, then the persona cards):

```bash
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
