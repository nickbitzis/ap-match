import pytest

from apmatch.config import TERMS
from apmatch.model import from_date, to_date
from apmatch.payments import (
    annualized_return,
    decide,
    is_run_day,
    last_run_on_or_before,
    next_run_on_or_after,
    plan,
    proposal,
)

from .helpers import supplier


def test_annualized_return_math():
    assert annualized_return(TERMS["2/10 net 30"]) == pytest.approx(0.02 / 0.98 * 365 / 20)  # 37.24%
    assert annualized_return(TERMS["2/10 net 30"]) == pytest.approx(0.3724, abs=1e-4)
    assert annualized_return(TERMS["1/10 net 30"]) == pytest.approx(0.1843, abs=1e-4)
    assert annualized_return(TERMS["0.5/10 net 45"]) == pytest.approx(0.0524, abs=1e-4)
    assert annualized_return(TERMS["net 30"]) == 0.0


def test_run_days_are_tuesday_and_thursday():
    days = [d for d in range(14) if is_run_day(d)]
    assert [to_date(d).strftime("%a") for d in days] == ["Thu", "Tue", "Thu", "Tue"]
    assert last_run_on_or_before(12.7, 0) == 8  # Monday Jan 13 -> Thursday Jan 9
    assert next_run_on_or_after(10) == 13  # Saturday Jan 11 -> Tuesday Jan 14
    assert last_run_on_or_before(5, 3) is None


def test_take_discount_on_last_run_inside_the_window():
    # Invoice dated Jan 6 (day 5), 2/10 net 30: the discount date is Jan 16 (day 15, a Thursday).
    d = decide(10_000, 5, 6, TERMS["2/10 net 30"])
    assert d.take_discount and d.pay_day == 15 and d.discount == 200.0 and "37.2%" in d.reason


def test_decline_discount_below_cost_of_cash():
    d = decide(10_000, 5, 6, TERMS["0.5/10 net 45"], cost_of_cash=0.08)
    assert not d.take_discount and d.reason.startswith("declined") and d.pay_day <= 50
    assert decide(10_000, 5, 6, TERMS["0.5/10 net 45"], cost_of_cash=0.04).take_discount


def test_missed_discount_when_approved_late():
    d = decide(10_000, 5, 17, TERMS["2/10 net 30"])
    assert not d.take_discount and d.discount_available and d.reason.startswith("missed")
    assert d.pay_day == last_run_on_or_before(35, 17)


def test_no_discount_pays_on_last_run_before_due():
    d = decide(10_000, 5, 6, TERMS["net 30"])
    assert d.pay_day == last_run_on_or_before(35, 6) and not d.late


def test_late_payment_when_approved_after_due_date():
    d = decide(10_000, 5, 40, TERMS["net 30"])
    assert d.late and d.pay_day == next_run_on_or_after(40) and "paid late" in d.reason


def mk(i, sup, amount, inv_day, appr_day):
    return {
        "id": i,
        "supplier_id": sup,
        "number": i,
        "status": "approved",
        "approved_amount": amount,
        "invoice_day": inv_day,
        "approved_day": appr_day,
    }


def test_plan_totals_dpo_and_credit_netting():
    sups = {"A": supplier("A", terms="2/10 net 30"), "B": supplier("B", terms="net 30")}
    docs = [
        mk("1", "A", 1000.0, 5, 6),
        mk("2", "A", 1000.0, 5, 20),
        mk("3", "B", 500.0, 5, 6),
        mk("4", "A", -100.0, 8, 9),
    ]
    p = plan(docs, sups, as_of=60)
    assert p["discount_captured"] == 20.0 and p["discount_missed"] == 20.0 and p["capture_rate"] == 0.5
    assert all(d["status"] == "paid" for d in docs)
    # DPO: amount-weighted days from invoice to payment over paid invoices.
    expect = (
        1000 * (15 - 5) + 1000 * (docs[1]["payment"]["pay_day"] - 5) + 500 * (docs[2]["payment"]["pay_day"] - 5)
    ) / 2500
    assert p["dpo_days"] == pytest.approx(expect, abs=0.05)
    run = proposal(docs, sups, docs[3]["payment"]["pay_day"])
    a = next(x for x in run["payments"] + run["carried_forward"] if x["supplier_id"] == "A")
    assert -100.0 in [i["amount"] for i in a["invoices"]]


def test_future_runs_are_proposed_not_paid():
    sups = {"A": supplier("A", terms="net 60")}
    docs = [mk("1", "A", 1000.0, 300, 301)]
    p = plan(docs, sups, as_of=310)
    assert docs[0]["status"] == "scheduled" and p["runs"][0]["status"] == "proposed"
    assert p["dpo_days"] is None


def test_date_round_trip():
    assert from_date(to_date(72)) == 72.0
