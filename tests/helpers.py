"""Hand-built suppliers, POs, receipts, and invoices for focused tests."""

from __future__ import annotations

import random

from apmatch.engine import APSystem
from apmatch.generator import make_people
from apmatch.layouts import render
from apmatch.model import Invoice, InvoiceLine, POLine, PurchaseOrder, Receipt, Supplier


def supplier(sid="S0001", category="components", terms="2/10 net 30", layout="block", taxable=False) -> Supplier:
    return Supplier(
        sid,
        f"Test {sid} Fasteners LLC",
        category,
        terms,
        "111000025",
        "1234567890",
        layout,
        "INV-{:06d}",
        taxable,
        0.0625,
    )


def po(po_id=1, sid="S0001", category="components", lines=None, creator="B01") -> PurchaseOrder:
    lines = lines or [POLine(1, "CP-1001", "Hex bolt M8x30", 100.0, 2.00, "EA", category)]
    return PurchaseOrder(po_id, sid, creator, 0.0, category, lines)


def receipt(rid, po_id, line_no, day, qty, returned=0.0) -> Receipt:
    return Receipt(rid, po_id, line_no, day, qty, returned, "R01")


def invoice(
    sup: Supplier,
    po_id,
    items,
    number="INV-000101",
    day=10.0,
    charges=(),
    credit=False,
    original=None,
    routing=None,
    account=None,
    layout=None,
) -> Invoice:
    lines = []
    for item, desc, q, p in items:
        lines.append(InvoiceLine(len(lines) + 1, item, desc, q, p, round(q * p, 2)))
    for kind, amt in charges:
        lines.append(InvoiceLine(len(lines) + 1, None, kind.title(), None, None, amt, kind))
    inv = Invoice(
        "X",
        sup.id,
        number,
        day,
        day + 0.5,
        po_id,
        lines,
        round(sum(x.amount for x in lines), 2),
        routing or sup.routing,
        account or sup.account,
        layout or sup.layout,
        is_credit=credit,
        original_number=original,
    )
    inv.text = render(inv, sup)
    return inv


def credit_line(sup, number, original, amount, day=20.0):
    line = InvoiceLine(1, None, f"Credit for {original}", None, None, amount)
    inv = Invoice(
        "X",
        sup.id,
        number,
        day,
        day + 0.5,
        None,
        [line],
        amount,
        sup.routing,
        sup.account,
        sup.layout,
        is_credit=True,
        original_number=original,
    )
    inv.text = render(inv, sup)
    return inv


def system(suppliers, pos, receipts) -> APSystem:
    people, _ = make_people(random.Random(1))
    return APSystem({s.id: s for s in suppliers}, people, {p.id: p for p in pos}, receipts)


def doc(items, total=None, charges=(), po_number=1, doc_type="invoice") -> dict:
    lines = [
        {
            "line_no": i + 1,
            "item": it,
            "description": d,
            "qty": q,
            "unit_price": p,
            "amount": round(q * p, 2),
            "kind": "item",
        }
        for i, (it, d, q, p) in enumerate(items)
    ]
    for kind, amt in charges:
        lines.append(
            {
                "line_no": len(lines) + 1,
                "item": None,
                "description": kind,
                "qty": None,
                "unit_price": None,
                "amount": amt,
                "kind": kind,
            }
        )
    return {
        "lines": lines,
        "total": total if total is not None else round(sum(x["amount"] for x in lines), 2),
        "po_number": po_number,
        "doc_type": doc_type,
    }
