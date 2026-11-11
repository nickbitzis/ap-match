import pytest

from apmatch.controls import SoDViolation
from apmatch.engine import ActionError
from apmatch.model import POLine

from .helpers import credit_line, invoice, po, receipt, supplier, system

BOLT = ("CP-1001", "Hex bolt M8x30")


def setup(rcv_qty=100.0, category="components", creator="B01", taxable=False, po_lines=None):
    sup = supplier(category=category, taxable=taxable)
    p = po(category=category, creator=creator, lines=po_lines)
    rc = [receipt("R1", 1, 1, 5.0, rcv_qty)] if rcv_qty else []
    return sup, p, system([sup], [p], rc)


def ingest(s, inv, at=None):
    return s.ingest(inv.text, at if at is not None else inv.received_day)


def types(s, d):
    return sorted(s.exceptions[x]["type"] for x in d["exceptions"])


def test_clean_invoice_is_touchless():
    sup, p, s = setup()
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)]))
    assert d["status"] == "approved" and d["touchless"] and d["approved_amount"] == 200.0
    assert [a["action"] for a in s.audit] == ["ingest", "approved"]


@pytest.mark.parametrize(
    "items,rcv,expected",
    [
        ([(*BOLT, 100, 2.5)], 100.0, ["price"]),
        ([(*BOLT, 120, 2.0)], 100.0, ["quantity"]),
        ([(*BOLT, 100, 2.0)], 0.0, ["no_receipt"]),
    ],
)
def test_line_exceptions_are_raised_with_owners(items, rcv, expected):
    sup, p, s = setup(rcv_qty=rcv)
    d = ingest(s, invoice(sup, 1, items))
    assert d["status"] == "exception" and types(s, d) == expected
    e = s.exceptions[d["exceptions"][0]]
    assert e["owner"] == {"price": "buyer", "quantity": "receiving", "no_receipt": "receiving"}[expected[0]]
    assert e["due_day"] - e["created_day"] == pytest.approx(e["sla_hours"] / 24)


def test_duplicate_with_reformatted_number_is_held():
    sup, p, s = setup()
    ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)], number="INV-000101"))
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)], number="inv 101", day=14.0))
    assert types(s, d) == ["duplicate"] and d["duplicate_of"]
    assert s.billed[(1, 1)] == 100  # the duplicate did not consume receipt quantity


def test_bank_change_raises_and_is_verified_by_two_people():
    sup, p, s = setup()
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)], routing="999999999", account="5550001111"))
    e = s.exceptions[d["exceptions"][0]]
    assert e["type"] == "bank_change" and e["owner"] == "treasury"
    with pytest.raises(ActionError):
        s.act(e["id"], "approve_bank_change", "T02", 11.0)  # no callback yet
    s.act(e["id"], "callback_confirmed", "T01", 11.0, note="called number on file")
    with pytest.raises(SoDViolation):
        s.act(e["id"], "approve_bank_change", "T01", 11.2)
    s.act(e["id"], "approve_bank_change", "T02", 11.3)
    assert s.suppliers[sup.id].account == "5550001111" and s.suppliers[sup.id].bank_history
    assert d["status"] == "approved" and not d["touchless"]


def test_failed_callback_rejects_the_invoice():
    sup, p, s = setup()
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)], routing="999999999", account="5550001111"))
    s.act(d["exceptions"][0], "callback_failed", "T01", 11.0)
    assert d["status"] == "rejected" and s.suppliers[sup.id].account == "1234567890"
    assert s.billed[(1, 1)] == 0


def test_closed_po_is_flagged():
    sup, p, s = setup()
    p.status, p.closed_day = "closed", 3.0
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)]))
    assert types(s, d) == ["closed_po"]


def test_non_po_invoice_and_unknown_po():
    sup, p, s = setup()
    assert types(s, ingest(s, invoice(sup, None, [("X", "Repair call-out", 1, 300.0)]))) == ["non_po"]
    assert types(s, ingest(s, invoice(sup, 4242, [(*BOLT, 1, 2.0)], number="INV-9"))) == ["non_po"]


def test_freight_over_limit_is_non_po():
    sup, p, s = setup()
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)], charges=[("freight", 300.0)]))
    assert types(s, d) == ["non_po"]


def test_credit_memo_matches_original_and_is_touchless():
    sup, p, s = setup()
    ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)], number="INV-000101"))
    cm = ingest(s, credit_line(sup, "CM77", "INV-000101", -20.0))
    assert cm["status"] == "approved" and cm["approved_amount"] == -20.0
    orphan = ingest(s, credit_line(sup, "CM78", "INV-999999", -5.0))
    assert types(s, orphan) == ["non_po"]


def test_sod_po_creator_cannot_accept_variance():
    sup, p, s = setup(creator="S01")  # an AP supervisor raised this PO
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.1)]))  # $10 variance, 4.8%: AP supervisor level
    e = d["exceptions"][0]
    with pytest.raises(SoDViolation):
        s.act(e, "accept_variance", "S01", 11.0)
    route = s.approval_route(s.exceptions[e])
    assert route["approver"] == "S02"
    s.act(e, "accept_variance", "S02", 11.0)
    assert d["status"] == "approved" and d["approved_amount"] == 210.0


def test_approval_limit_is_enforced():
    big = [POLine(1, "CP-1001", "Hex bolt M8x30", 1000, 50.0, "EA", "components")]
    sup, p, s = setup(rcv_qty=1000, po_lines=big)
    d = ingest(s, invoice(sup, 1, [(*BOLT, 1000, 55.0)]))  # $5,000 variance on $55,000
    e = d["exceptions"][0]
    with pytest.raises(ActionError, match="cannot approve"):
        s.act(e, "accept_variance", "S01", 11.0)
    assert s.approval_route(s.exceptions[e])["role"] == "purchasing_manager"
    s.act(e, "accept_variance", "C01", 11.0)  # a higher level may always approve
    assert d["status"] == "approved"


def test_short_pay_pays_po_terms_and_settles_all_line_issues():
    sup, p, s = setup(rcv_qty=80)
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.5)]))
    assert types(s, d) == ["price", "quantity"]
    s.act(d["exceptions"][1], "short_pay", "R02", 11.0)
    assert d["status"] == "approved" and d["approved_amount"] == 160.0
    assert all(s.exceptions[x]["status"] == "resolved" for x in d["exceptions"])
    assert s.billed[(1, 1)] == 80


def test_receipt_posted_resolves_no_receipt():
    sup, p, s = setup(rcv_qty=0)
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)]))
    with pytest.raises(ActionError):
        s.act(d["exceptions"][0], "receipt_posted", "R01", 11.0)
    s.receipts[(1, 1)].append(receipt("R9", 1, 1, 12.0, 100))
    s.act(d["exceptions"][0], "receipt_posted", "R01", 12.5)
    assert d["status"] == "approved"


def test_not_duplicate_releases_and_rematches():
    sup, p, s = setup(rcv_qty=200, po_lines=[POLine(1, "CP-1001", "Hex bolt M8x30", 200, 2.0, "EA", "components")])
    ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)], number="INV-000101"))
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.0)], number="INV-000110", day=12.0))
    assert types(s, d) == ["duplicate"]
    s.act(d["exceptions"][0], "not_duplicate", "A01", 13.0, note="supplier confirmed two shipments")
    assert d["status"] == "approved" and s.billed[(1, 1)] == 200


def test_capture_exception_then_clerk_correction():
    sup, p, s = setup()
    inv = invoice(sup, 1, [(*BOLT, 100, 2.0)])
    d = s.ingest(inv.text.replace("Test S0001 Fasteners LLC", "Unknown Vendor"), 10.5)
    assert types(s, d) == ["capture"] and "match" not in d
    from apmatch.layouts import parse

    fields = parse(inv.text)
    s.act(d["exceptions"][0], "correct_fields", "A02", 10.7, fields=fields)
    assert d["status"] == "approved" and d["touches"] == ["capture"]


def test_invalid_actions_are_refused():
    sup, p, s = setup()
    d = ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.5)]))
    e = d["exceptions"][0]
    with pytest.raises(ActionError, match="not allowed"):
        s.act(e, "callback_confirmed", "T01", 11.0)
    with pytest.raises(ActionError, match="does not work"):
        s.act(e, "short_pay", "T01", 11.0)
    with pytest.raises(ActionError, match="unknown person"):
        s.act(e, "short_pay", "ZZ9", 11.0)
    with pytest.raises(KeyError):
        s.act("E999", "hold", "A01", 11.0)
    s.act(e, "hold", "A01", 11.0)
    assert d["status"] == "on_hold"


def test_queue_filters_and_sla_timer():
    sup, p, s = setup(rcv_qty=0)
    ingest(s, invoice(sup, 1, [(*BOLT, 100, 2.5)]))
    assert [e["type"] for e in s.queue(owner="receiving")] == ["no_receipt"]
    assert s.queue(etype="price")[0]["owner"] == "buyer"
    q = s.queue(now=10.5 + 3)  # three days later: price (48h) is overdue, no receipt (72h) is not yet
    assert {e["type"]: e["overdue"] for e in q} == {"price": True, "no_receipt": False}
