import pytest

from apmatch.layouts import LAYOUTS, detect, parse, parse_eu, parse_us, price_txt
from apmatch.model import Invoice, InvoiceLine

from .helpers import credit_line, invoice, supplier

ITEMS = [("CP-1001", "Hex bolt M8x30", 1200, 0.0825), ("CP-1002", "Lock nut M10", 40, 1234.5)]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_each_parser_reads_its_layout(layout):
    sup = supplier(layout=layout)
    inv = invoice(sup, 1234, ITEMS, number="INV-004512", day=72.0, charges=[("freight", 45.0), ("tax", 12.34)])
    assert detect(inv.text) == layout
    out = parse(inv.text)
    assert out["layout"] == layout
    assert out["invoice_number"] == "INV-004512"
    assert out["invoice_date"] == "2025-03-14"
    assert out["po_number"] == 1234
    assert (out["routing"], out["account"]) == (sup.routing, sup.account)
    assert out["total"] == pytest.approx(inv.total)
    items = [x for x in out["lines"] if x["kind"] == "item"]
    assert [(x["item"], x["qty"], x["unit_price"]) for x in items] == [(i, q, p) for i, _, q, p in ITEMS]
    assert [x["kind"] for x in out["lines"][2:]] == ["freight", "tax"]
    assert min(out["confidence"].values()) == 1.0


@pytest.mark.parametrize("layout", LAYOUTS)
def test_credit_memo_in_each_layout(layout):
    sup = supplier(layout=layout)
    cm = credit_line(sup, "CM5521", "INV-004512", -250.0)
    out = parse(cm.text)
    assert out["doc_type"] == "credit_memo"
    assert out["original_invoice"] == "INV-004512"
    assert out["total"] == -250.0


@pytest.mark.parametrize("layout", ["block", "euro", "letter"])
def test_invoice_number_with_a_space(layout):
    inv = invoice(supplier(layout=layout), 7, ITEMS[:1], number="INV 4512")
    assert parse(inv.text)["invoice_number"] == "INV 4512"


def test_ocr_letter_o_is_repaired_with_lower_confidence():
    sup = supplier(layout="letter")
    inv = Invoice(
        "X",
        sup.id,
        "77",
        10.0,
        10.5,
        9,
        [InvoiceLine(1, "CP-1", "Hex bolt", 10, 10.0, 100.0)],
        100.0,
        sup.routing,
        sup.account,
        "letter",
    )
    from apmatch.layouts import render

    text = render(inv, sup, {"ocr_o": 0})
    assert "$1OO.00" in text or "$1O0.00" in text
    out = parse(text)
    assert out["lines"][0]["amount"] == 100.0
    assert out["lines"][0]["confidence"] == 0.9


def test_dropped_decimal_drops_confidence_below_floor():
    sup = supplier(layout="letter")
    inv = Invoice(
        "X",
        sup.id,
        "77",
        10.0,
        10.5,
        9,
        [InvoiceLine(1, "CP-1", "Hex bolt", 10, 12.45, 124.5)],
        124.5,
        sup.routing,
        sup.account,
        "letter",
    )
    from apmatch.layouts import render

    out = parse(render(inv, sup, {"drop_decimal": 0}))
    assert out["lines"][0]["unit_price"] == 1245.0
    assert out["confidence"]["lines"] == 0.3


def test_total_mismatch_lowers_total_confidence():
    inv = invoice(supplier(layout="pipe"), 3, ITEMS[:1])
    text = inv.text.replace(f"TOTAL|{inv.total:.2f}", "TOTAL|999.99")
    assert parse(text)["confidence"]["total"] == 0.5


def test_number_helpers():
    assert parse_us("$1,234.50") == (1234.5, False)
    assert parse_us("1,2O4.5O") == (1204.5, True)
    assert parse_eu("1.234,56") == 1234.56
    assert price_txt(0.0825) == "0.0825" and price_txt(12.5) == "12.50" and price_txt(1234.5, ",") == "1.234,50"


def test_mangled_document_is_zero_confidence_not_a_crash():
    out = parse("DOC|INVOICE\nLINE|1|x|y|notanumber|1|1\n")
    assert out["confidence"]["supplier_name"] == 0.0 and "error" in out
