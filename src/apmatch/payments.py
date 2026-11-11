"""Payment optimization: take an early payment discount only when its annualized return beats the cost of cash,
otherwise pay on the last payment run before the due date. Payment runs are Tuesdays and Thursdays.

Annualized return of a discount (simple, not compounded):
    d / (1 - d) * 365 / (net_days - discount_days)
2/10 net 30 is 0.02 / 0.98 * 365 / 20 = 37.2% a year. Paying 20 days early to earn that beats almost any cost of
cash. 0.5/10 net 45 earns 5.2% a year, which loses to an 8% cost of cash, so the scheduler declines it."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .config import COST_OF_CASH, PAYMENT_RUN_WEEKDAYS, TERMS, Terms
from .model import EPOCH, to_date


def annualized_return(t: Terms) -> float:
    if t.discount_pct <= 0 or t.net_days <= t.discount_days:
        return 0.0
    return t.discount_pct / (1 - t.discount_pct) * 365 / (t.net_days - t.discount_days)


def is_run_day(day: int) -> bool:
    return (EPOCH.weekday() + day) % 7 in PAYMENT_RUN_WEEKDAYS


def last_run_on_or_before(day: float, not_before: float) -> int | None:
    """The latest run day in [not_before, day]. Runs happen at end of day, so an invoice approved at 10:00 on a run
    day makes that day's run."""
    d = math.floor(day)
    lo = math.floor(not_before)
    while d >= lo:
        if is_run_day(d):
            return d
        d -= 1
    return None


def next_run_on_or_after(day: float) -> int:
    d = math.floor(day)
    while not is_run_day(d):
        d += 1
    return d


@dataclass
class Decision:
    pay_day: int
    take_discount: bool
    discount: float
    annualized: float
    reason: str
    late: bool
    discount_available: bool  # terms offer a discount worth taking at this cost of cash


def decide(amount: float, invoice_day: float, approved_day: float, terms: Terms, cost_of_cash: float = COST_OF_CASH):
    due = invoice_day + terms.net_days
    ann = annualized_return(terms)
    worth = ann > cost_of_cash and amount > 0
    if worth:
        r = last_run_on_or_before(invoice_day + terms.discount_days, approved_day)
        if r is not None:
            disc = round(amount * terms.discount_pct, 2)
            return Decision(
                r,
                True,
                disc,
                ann,
                f"take {terms.code}: {ann:.1%} a year beats {cost_of_cash:.1%} cost of cash",
                False,
                True,
            )
    r = last_run_on_or_before(due, approved_day)
    late = r is None
    if late:
        r = next_run_on_or_after(approved_day)
    if worth:
        reason = "missed: approved after the discount date"
    elif terms.discount_pct > 0:
        reason = f"declined: {ann:.1%} a year is below the {cost_of_cash:.1%} cost of cash"
    else:
        reason = "no discount offered; pay on the last run before the due date"
    if late:
        reason += "; paid late, approved after the due date"
    return Decision(r, False, 0.0, ann, reason, late, worth)


def plan(docs: list[dict], suppliers: dict, as_of: float, cost_of_cash: float = COST_OF_CASH) -> dict:
    """Schedule every approved document. Credit memos ride in the next run for their supplier and are netted
    against that supplier's payments in the run. Returns the per-document decisions, runs, and totals."""
    sched = []
    for d in docs:
        if d["status"] not in ("approved", "scheduled", "paid") or d.get("approved_day") is None:
            continue
        terms = TERMS[suppliers[d["supplier_id"]].terms]
        amt = float(d["approved_amount"])
        if amt < 0:
            r = next_run_on_or_after(d["approved_day"])
            dec = Decision(r, False, 0.0, 0.0, "credit memo: netted in the next run", False, False)
        else:
            dec = decide(amt, d["invoice_day"], d["approved_day"], terms, cost_of_cash)
        sched.append((d, dec, amt))
    runs: dict[int, dict] = defaultdict(lambda: {"by_supplier": defaultdict(float), "invoices": 0, "discount": 0.0})
    captured = missed = declined = available = 0.0
    weighted_days = paid_amt = 0.0
    late = 0
    for d, dec, amt in sched:
        run = runs[dec.pay_day]
        run["by_supplier"][d["supplier_id"]] += amt - dec.discount
        run["invoices"] += 1
        run["discount"] += dec.discount
        d["payment"] = {
            "pay_date": to_date(dec.pay_day).isoformat(),
            "pay_day": dec.pay_day,
            "take_discount": dec.take_discount,
            "discount": dec.discount,
            "annualized_return": round(dec.annualized, 4),
            "reason": dec.reason,
            "late": dec.late,
            "net_amount": round(amt - dec.discount, 2),
        }
        d["status"] = "paid" if dec.pay_day <= as_of else "scheduled"
        if amt > 0:
            terms = TERMS[suppliers[d["supplier_id"]].terms]
            pot = amt * terms.discount_pct
            if dec.discount_available:
                available += pot
                if dec.take_discount:
                    captured += dec.discount
                else:
                    missed += pot
            elif terms.discount_pct > 0:
                declined += pot
            if dec.pay_day <= as_of:
                weighted_days += amt * (dec.pay_day - d["invoice_day"])
                paid_amt += amt
                late += dec.late
    run_rows = []
    for day in sorted(runs):
        r = runs[day]
        net = {k: round(v, 2) for k, v in r["by_supplier"].items()}
        run_rows.append(
            {
                "date": to_date(day).isoformat(),
                "day": day,
                "status": "paid" if day <= as_of else "proposed",
                "invoices": r["invoices"],
                "suppliers": len(net),
                "amount": round(sum(v for v in net.values() if v > 0), 2),
                "discount": round(r["discount"], 2),
                "carried_credit": round(-sum(v for v in net.values() if v < 0), 2),
            }
        )
    return {
        "cost_of_cash": cost_of_cash,
        "discount_available": round(available, 2),
        "discount_captured": round(captured, 2),
        "discount_missed": round(missed, 2),
        "discount_declined": round(declined, 2),
        "capture_rate": round(captured / available, 4) if available else None,
        "dpo_days": round(weighted_days / paid_amt, 1) if paid_amt else None,
        "paid_late": late,
        "runs": run_rows,
    }


def proposal(docs: list[dict], suppliers: dict, run_date_day: int) -> dict:
    """The payment file for one run: one payment per supplier, net of credit memos and discounts."""
    rows: dict[str, dict] = {}
    for d in docs:
        p = d.get("payment")
        if not p or p["pay_day"] != run_date_day:
            continue
        r = rows.setdefault(
            d["supplier_id"],
            {
                "supplier_id": d["supplier_id"],
                "supplier": suppliers[d["supplier_id"]].name,
                "invoices": [],
                "gross": 0.0,
                "discount": 0.0,
                "net": 0.0,
            },
        )
        r["invoices"].append(
            {
                "id": d["id"],
                "number": d["number"],
                "amount": d["approved_amount"],
                "discount": p["discount"],
                "reason": p["reason"],
            }
        )
        r["gross"] = round(r["gross"] + d["approved_amount"], 2)
        r["discount"] = round(r["discount"] + p["discount"], 2)
        r["net"] = round(r["gross"] - r["discount"], 2)
    pays = sorted(rows.values(), key=lambda x: -x["net"])
    return {
        "run_date": to_date(run_date_day).isoformat(),
        "payments": [p for p in pays if p["net"] > 0],
        "carried_forward": [p for p in pays if p["net"] <= 0],
        "total": round(sum(p["net"] for p in pays if p["net"] > 0), 2),
        "discount": round(sum(p["discount"] for p in pays), 2),
    }
