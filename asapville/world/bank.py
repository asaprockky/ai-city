"""AsapBank: savings that earn interest, loans that cost it.

The bank's vault is the town fund: deposits go into it, loans come out of it, and it pays
and charges interest once a day. A citizen may borrow up to a credit limit set by what they
earned lately and the businesses they own. A loan is due a week after it is taken; when it
is overdue the bank takes the borrower's savings and cash, then forecloses on a business
they own, and posts the default on the town board.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.config import Settings
from asapville.db.models import Agent, BankAccount, Event, Location, WorldState
from asapville.world.events import emit, remember
from asapville.world.social import nudge_reputation

INCOME_WINDOW = 72  # hours of wages that count toward credit


def account(session: Session, agent: Agent, tick: int = 0, *, create: bool = True) -> BankAccount | None:
    acc = session.get(BankAccount, agent.id)
    if acc is None and create:
        acc = BankAccount(agent_id=agent.id, deposit_cents=0, loan_cents=0, defaulted=False,
                          opened_tick=tick)
        session.add(acc)
        session.flush()
    return acc


def _fund(session: Session) -> WorldState:
    ws = session.get(WorldState, 1)
    assert ws is not None
    return ws


def recent_income(session: Session, agent: Agent, tick: int) -> int:
    total = 0
    for e in session.scalars(select(Event).where(Event.kind == "work", Event.agent_id == agent.id,
                                                 Event.tick >= tick - INCOME_WINDOW)):
        total += int((e.data or {}).get("wage_cents") or 0)
    return total


def collateral(session: Session, settings: Settings, agent: Agent) -> int:
    from asapville.world.economy import business_price, is_business

    owned = session.scalars(select(Location).where(Location.owner_id == agent.id))
    return sum(business_price(settings, loc) for loc in owned if is_business(settings, loc))


def credit_limit(session: Session, settings: Settings, agent: Agent, tick: int) -> int:
    """How much this citizen may owe the bank in all."""
    cfg = settings.bank
    acc = account(session, agent, tick, create=False)
    if acc is not None and acc.defaulted:
        return 0
    has_job = agent.role in settings.economy.jobs
    limit = (cfg.min_credit_cents if has_job else 0) + int(cfg.income_multiple * recent_income(
        session, agent, tick)) + int(cfg.collateral_share * collateral(session, settings, agent))
    return limit


def can_borrow(session: Session, settings: Settings, agent: Agent, tick: int) -> int:
    acc = account(session, agent, tick, create=False)
    owed = acc.loan_cents if acc else 0
    return max(0, min(credit_limit(session, settings, agent, tick) - owed, _fund(session).treasury_cents))


def deposit(session: Session, agent: Agent, cents: int, tick: int) -> None:
    acc = account(session, agent, tick)
    assert acc is not None
    agent.money_cents -= cents
    acc.deposit_cents += cents
    _fund(session).treasury_cents += cents


def withdraw(session: Session, agent: Agent, cents: int, tick: int) -> None:
    acc = account(session, agent, tick)
    assert acc is not None
    acc.deposit_cents -= cents
    agent.money_cents += cents
    _fund(session).treasury_cents -= cents


def borrow(session: Session, settings: Settings, agent: Agent, cents: int, tick: int) -> None:
    acc = account(session, agent, tick)
    assert acc is not None
    if acc.loan_cents == 0:
        acc.loan_due_tick = tick + settings.bank.loan_days * settings.clock.ticks_per_day
    acc.loan_cents += cents
    agent.money_cents += cents
    _fund(session).treasury_cents -= cents


def repay(session: Session, agent: Agent, cents: int, tick: int) -> int:
    acc = account(session, agent, tick)
    assert acc is not None
    cents = min(cents, acc.loan_cents)
    agent.money_cents -= cents
    acc.loan_cents -= cents
    _fund(session).treasury_cents += cents
    if acc.loan_cents == 0:
        acc.loan_due_tick = None
        acc.defaulted = False
    return cents


def describe(session: Session, settings: Settings, agent: Agent, tick: int) -> str | None:
    """One line for the citizen's own perception, or None if they have no account."""
    acc = account(session, agent, tick, create=False)
    if acc is None or (acc.deposit_cents == 0 and acc.loan_cents == 0):
        return None
    cfg = settings.bank
    parts = []
    if acc.deposit_cents:
        parts.append(f"savings {acc.deposit_cents / 100:.2f} asaps (earning {cfg.deposit_rate:.1%} a day)")
    if acc.loan_cents:
        due = acc.loan_due_tick
        when = (f"due in {max(0, due - tick)} hours" if due is not None and due > tick else "OVERDUE")
        parts.append(f"a loan of {acc.loan_cents / 100:.2f} asaps ({cfg.loan_rate:.0%} a day, {when})"
                     + (" — you defaulted; the bank lends you nothing more" if acc.defaulted else ""))
    return "Your AsapBank account: " + "; ".join(parts) + "."


def daily(session: Session, settings: Settings, tick: int) -> None:
    """Pay interest on savings, charge it on loans, and collect what is overdue."""
    cfg = settings.bank
    ws = _fund(session)
    for acc in session.scalars(select(BankAccount)):
        agent = session.get(Agent, acc.agent_id)
        if agent is None:
            continue
        if acc.deposit_cents and agent.alive:
            interest = int(acc.deposit_cents * cfg.deposit_rate)
            acc.deposit_cents += interest
            ws.treasury_cents -= interest
        if acc.loan_cents:
            acc.loan_cents += max(1, int(acc.loan_cents * cfg.loan_rate))
            if acc.loan_due_tick is not None and tick >= acc.loan_due_tick:
                _collect(session, settings, agent, acc, tick)


def _collect(session: Session, settings: Settings, agent: Agent, acc: BankAccount, tick: int) -> None:
    from asapville.world.economy import business_price, is_business

    ws = _fund(session)
    taken = []
    if acc.deposit_cents:
        n = min(acc.deposit_cents, acc.loan_cents)
        acc.deposit_cents -= n
        acc.loan_cents -= n
        taken.append(f"{n / 100:.2f} from savings")
    if acc.loan_cents and agent.money_cents > 0:
        n = min(agent.money_cents, acc.loan_cents)
        agent.money_cents -= n
        acc.loan_cents -= n
        ws.treasury_cents += n
        taken.append(f"{n / 100:.2f} in cash")
    if acc.loan_cents:
        for loc in session.scalars(select(Location).where(Location.owner_id == agent.id)):
            if not is_business(settings, loc) or acc.loan_cents <= 0:
                continue
            value = int(business_price(settings, loc) * settings.bank.collateral_share)
            loc.owner_id = None  # back to the town, which runs it again
            acc.loan_cents = max(0, acc.loan_cents - value)
            taken.append(f"{loc.name} (counted at {value / 100:.2f})")
    if acc.loan_cents == 0:
        acc.loan_due_tick = None
        summary = f"AsapBank collected {agent.name}'s overdue loan: " + ", ".join(taken)
    else:
        acc.defaulted = True
        nudge_reputation(agent, -0.2)
        summary = (f"{agent.name} defaulted on an AsapBank loan; the bank took "
                   + (", ".join(taken) or "nothing") + f" and is still owed {acc.loan_cents / 100:.2f}")
    emit(session, tick=tick, kind="notice", actor=agent, visibility="public", summary=summary,
         data={"text": summary, "author": "AsapBank", "loan_left": acc.loan_cents}, importance=6)
    remember(session, agent.id, tick, "outcome",
             f"My AsapBank loan was overdue. The bank took {', '.join(taken) or 'nothing'}"
             + (f"; I still owe {acc.loan_cents / 100:.2f} and it will lend me nothing more."
                if acc.loan_cents else "; the debt is settled."), importance=8)
