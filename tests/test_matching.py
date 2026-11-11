import pytest

from apmatch.config import TOLERANCES
from apmatch.matching import check_price, jaccard, match
from apmatch.model import POLine

from .helpers import doc, po, receipt, supplier

COMP = TOLERANCES["components"]  # 1% and $50, no over-delivery
RAW = TOLERANCES["raw_materials"]  # 2% and $150, 5% over-delivery


def run(d, p, sup=None, rcpts=(), prior=None, at=100.0):
    by = {}
    for r in rcpts:
        by.setdefault((r.po_id, r.line_no), []).append(r)
    return match(d, p, sup or supplier(), by, prior or {}, at)


# ---- tolerance rules


def test_price_within_both_tolerances_passes():
    ok, pct, amt = check_price(2.015, 2.00, 100, COMP)
    assert ok and pct == pytest.approx(0.0075) and amt == 1.5


def test_price_over_percent_fails_even_if_dollars_are_small():
    ok, pct, _ = check_price(2.03, 2.00, 10, COMP)
    assert not ok and pct == pytest.approx(0.015)


def test_price_over_dollar_cap_fails_even_if_percent_is_small():
    ok, pct, amt = check_price(100.5, 100.0, 200, COMP)  # 0.5% but $100 on the line
    assert not ok and pct < COMP.price_pct and amt == 100.0


def test_billing_under_po_price_always_passes():
    assert check_price(1.50, 2.00, 1000, COMP)[0]


def test_services_tolerate_only_rounding():
    tol = TOLERANCES["services"]
    assert check_price(100.004, 100.0, 1, tol)[0]
    assert not check_price(100.5, 100.0, 4, tol)[0]
    assert tol.three_way is False


# ---- three-way quantity


def test_clean_three_way_match_explains_itself():
    p = po()
    r = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)]), p, rcpts=[receipt("R1", 1, 1, 5, 100)])
    assert r.mode == "three_way" and not r.issues
    ln = r.lines[0]
    assert ln.status == "ok" and ln.received_qty == 100 and ln.receipts == ["R1"]
    assert "within limit 1% and $50" in ln.explanation and "quantity OK" in ln.explanation
    assert r.summary.startswith("Three-way match: all 1 PO lines within tolerance")


def test_partial_receipt_blocks_billing_for_the_rest():
    p = po()
    r = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)]), p, rcpts=[receipt("R1", 1, 1, 5, 60)])
    ln = r.lines[0]
    assert ln.status == "quantity" and ln.qty_var == 40 and ln.payable_qty == 60
    assert r.payable_amount == 120.0 and r.variance_amount == 80.0
    assert "40 too many" in ln.explanation


def test_one_po_line_to_many_receipts():
    p = po()
    rc = [receipt("R1", 1, 1, 5, 60), receipt("R2", 1, 1, 12, 40)]
    r = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)]), p, rcpts=rc)
    assert not r.issues and r.lines[0].receipts == ["R1", "R2"]


def test_receipts_after_match_time_do_not_count():
    p = po()
    rc = [receipt("R1", 1, 1, 5, 60), receipt("R2", 1, 1, 30, 40)]
    r = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)]), p, rcpts=rc, at=20.0)
    assert r.lines[0].status == "quantity"


def test_second_invoice_against_partial_receipts_uses_prior_billed():
    p = po()
    rc = [receipt("R1", 1, 1, 5, 60), receipt("R2", 1, 1, 12, 40)]
    ok = run(doc([("CP-1001", "Hex bolt M8x30", 40, 2.0)]), p, rcpts=rc, prior={(1, 1): 60})
    assert not ok.issues
    over = run(doc([("CP-1001", "Hex bolt M8x30", 60, 2.0)]), p, rcpts=rc, prior={(1, 1): 60})
    assert over.lines[0].status == "quantity" and over.lines[0].qty_var == 20


def test_damaged_goods_returned_count_net():
    p = po()
    r = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)]), p, rcpts=[receipt("R1", 1, 1, 5, 100, returned=8)])
    assert r.lines[0].received_qty == 92 and r.lines[0].status == "quantity"
    assert "8 returned" in r.lines[0].explanation


def test_over_delivery_within_category_tolerance():
    p = po(category="raw_materials", lines=[POLine(1, "RM-1", "Steel sheet", 100, 5.0, "LB", "raw_materials")])
    r = run(doc([("RM-1", "Steel sheet", 104, 5.0)]), p, rcpts=[receipt("R1", 1, 1, 5, 104)])
    assert not r.issues


def test_over_delivery_beyond_tolerance_is_capped():
    p = po(category="raw_materials", lines=[POLine(1, "RM-1", "Steel sheet", 100, 5.0, "LB", "raw_materials")])
    r = run(doc([("RM-1", "Steel sheet", 110, 5.0)]), p, rcpts=[receipt("R1", 1, 1, 5, 110)])
    assert r.lines[0].status == "quantity" and r.lines[0].payable_qty == pytest.approx(105)


def test_no_receipt():
    r = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)]), po())
    assert ("no_receipt" in {t for t, _ in r.issues}) and r.payable_amount == 0.0
    assert "no goods receipt" in r.lines[0].explanation


# ---- line assignment


def test_many_invoice_lines_to_one_po_line():
    p = po()
    d = doc([("CP-1001", "Hex bolt M8x30", 50, 2.0), ("CP-1001", "Hex bolt M8x30", 50, 2.0)])
    r = run(d, p, rcpts=[receipt("R1", 1, 1, 5, 100)])
    assert len(r.lines) == 1 and r.lines[0].invoice_lines == [1, 2] and r.lines[0].billed_qty == 100
    assert not r.issues and "across 2 invoice lines" in r.lines[0].explanation


def test_description_fallback_when_item_code_missing():
    p = po()
    r = run(doc([(None, "hex bolt m8x30 zinc", 100, 2.0)]), p, rcpts=[receipt("R1", 1, 1, 5, 100)])
    assert r.lines and r.lines[0].po_line == 1
    assert jaccard("Hex bolt M8x30", "hex bolt m8x30 zinc") == 0.75


def test_line_not_on_po_is_non_po():
    p = po()
    r = run(
        doc([("CP-1001", "Hex bolt M8x30", 100, 2.0), ("ZZ-9", "Gold plating", 1, 400.0)]),
        p,
        rcpts=[receipt("R1", 1, 1, 5, 100)],
    )
    assert [t for t, _ in r.issues] == ["non_po"] and r.unmatched[0]["item"] == "ZZ-9"


def test_multi_line_po_mixed_results():
    lines = [
        POLine(1, "CP-1", "Bolt", 10, 1.0, "EA", "components"),
        POLine(2, "CP-2", "Nut", 10, 1.0, "EA", "components"),
    ]
    p = po(lines=lines)
    r = run(
        doc([("CP-1", "Bolt", 10, 1.0), ("CP-2", "Nut", 10, 1.5)]),
        p,
        rcpts=[receipt("R1", 1, 1, 5, 10), receipt("R2", 1, 2, 5, 10)],
    )
    assert [ln.status for ln in r.lines] == ["ok", "price"]
    assert r.summary.startswith("Three-way match: 1 of 2 PO lines clean")


# ---- two-way, charges, credit memos, non-PO


def test_two_way_for_services():
    p = po(category="services", lines=[POLine(1, "SV-1", "Field engineering", 40, 120.0, "HR", "services")])
    sup = supplier(category="services")
    assert not run(doc([("SV-1", "Field engineering", 40, 120.0)]), p, sup).issues
    r = run(doc([("SV-1", "Field engineering", 48, 120.0)]), p, sup)
    assert r.mode == "two_way" and r.lines[0].status == "quantity" and r.lines[0].received_qty is None


def test_small_freight_auto_accepted_big_freight_flagged():
    p, rc = po(), [receipt("R1", 1, 1, 5, 100)]
    small = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)], charges=[("freight", 40.0)]), p, rcpts=rc)
    assert not small.issues and small.payable_amount == 240.0
    big = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)], charges=[("freight", 400.0)]), p, rcpts=rc)
    assert [t for t, _ in big.issues] == ["non_po"] and big.payable_amount == 200.0


def test_tax_checked_against_supplier_rate():
    p, rc = po(), [receipt("R1", 1, 1, 5, 100)]
    taxable = supplier(taxable=True)
    ok = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)], charges=[("tax", 12.5)]), p, taxable, rc)
    assert not ok.issues
    wrong = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)], charges=[("tax", 30.0)]), p, taxable, rc)
    assert wrong.issues[0][0] == "non_po" and wrong.payable_amount == 212.5
    exempt = run(doc([("CP-1001", "Hex bolt M8x30", 100, 2.0)], charges=[("tax", 12.5)]), p, supplier(), rc)
    assert "not taxable" in exempt.issues[0][1]


def test_credit_memo_and_non_po_modes():
    cm = run({**doc([], total=-50.0), "doc_type": "credit_memo", "original_invoice": "INV-1"}, po())
    assert cm.mode == "credit_memo" and cm.payable_amount == -50.0 and not cm.issues
    npo = run({**doc([("X", "Repair call-out", 1, 300.0)]), "po_number": None}, None)
    assert npo.mode == "non_po" and npo.issues[0][0] == "non_po" and "no PO number" in npo.summary
