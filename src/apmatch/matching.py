"""Two-way and three-way matching at line level, with an explanation for every result.

Three-way: invoice line vs PO line (price) vs goods receipts (quantity). Two-way (services): invoice vs PO only.
Invoice lines are assigned to PO lines by item code, then by description similarity. Several invoice lines can land
on one PO line (the supplier split a line into lots: many-to-one), and one PO line can have several receipts
(partial deliveries: one-to-many). Quantities already billed on earlier invoices are subtracted."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .config import FREIGHT_AUTO_ACCEPT, TAX_ROUNDING, TOLERANCES, Tolerance
from .model import PurchaseOrder, Receipt, Supplier

EPS = 1e-6


def tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def jaccard(a: str, b: str) -> float:
    x, y = tokens(a), tokens(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def fmt_qty(q: float) -> str:
    return f"{q:,.0f}" if abs(q - round(q)) < EPS else f"{q:,.2f}"


@dataclass
class LineMatch:
    po_line: int
    item: str
    description: str
    invoice_lines: list[int]
    billed_qty: float
    invoice_price: float
    po_qty: float
    po_price: float
    received_qty: float | None  # net of returns, None for two-way
    receipts: list[str]
    prior_billed: float
    price_var_pct: float
    price_var_amount: float
    qty_var: float
    payable_qty: float
    status: str  # ok | price | quantity | no_receipt (a line can carry two issues; status shows the first)
    issues: list[str]
    explanation: str


@dataclass
class ChargeMatch:
    invoice_line: int
    kind: str
    amount: float
    status: str  # ok | non_po
    explanation: str


@dataclass
class MatchResult:
    mode: str  # three_way | two_way | non_po | credit_memo
    lines: list[LineMatch] = field(default_factory=list)
    charges: list[ChargeMatch] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    issues: list[tuple[str, str]] = field(default_factory=list)  # (exception type, detail)
    invoice_total: float = 0.0
    payable_amount: float = 0.0
    variance_amount: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["issues"] = [{"type": t, "detail": x} for t, x in self.issues]
        return d


def issue_text(t: str, ln: LineMatch, tol: Tolerance) -> str:
    """One short sentence per problem line, for the exceptions queue. The full reasoning stays on the match."""
    head = f"Line {ln.po_line} {ln.item}:"
    if t == "price":
        return (
            f"{head} billed ${ln.invoice_price:,.4f} vs PO ${ln.po_price:,.4f}, {ln.price_var_pct:+.2%} "
            f"(${ln.price_var_amount:,.2f}); limit {tol.price_pct:.0%} and ${tol.price_abs:,.0f}."
        )
    if t == "quantity":
        basis = "ordered" if ln.received_qty is None else "received net"
        have = ln.po_qty if ln.received_qty is None else ln.received_qty
        return (
            f"{head} billed {fmt_qty(ln.billed_qty)}, {basis} {fmt_qty(have)}, {fmt_qty(ln.prior_billed)} billed "
            f"before: {fmt_qty(ln.qty_var)} too many."
        )
    return f"{head} {fmt_qty(ln.billed_qty)} billed and no goods receipt posted."


def assign_lines(inv_lines: list[dict], po: PurchaseOrder) -> tuple[dict[int, list[dict]], list[dict]]:
    """Item code first, then description similarity of at least 0.5. Returns PO line -> invoice lines, and the
    invoice lines that match nothing on the PO."""
    by_item = {ln.item: ln.line_no for ln in po.lines}
    groups: dict[int, list[dict]] = {}
    unmatched = []
    for il in inv_lines:
        target = by_item.get(il.get("item") or "")
        if target is None:
            best = max(po.lines, key=lambda p: jaccard(il.get("description", ""), p.description), default=None)
            if best is not None and jaccard(il.get("description", ""), best.description) >= 0.5:
                target = best.line_no
        if target is None:
            unmatched.append(il)
        else:
            groups.setdefault(target, []).append(il)
    return groups, unmatched


def check_price(inv_price: float, po_price: float, qty: float, tol: Tolerance) -> tuple[bool, float, float]:
    """Over-billing passes only if it is within BOTH the percent and the absolute dollar tolerance.
    Billing under the PO price always passes. Returns (ok, variance percent, variance dollars on the line)."""
    diff = inv_price - po_price
    pct = diff / po_price if po_price else 0.0
    amount = round(diff * qty, 2)
    ok = diff <= EPS or (pct <= tol.price_pct + EPS and amount <= tol.price_abs + 0.005)
    return ok, pct, amount


def match(
    doc: dict,
    po: PurchaseOrder | None,
    supplier: Supplier,
    receipts: dict[tuple[int, int], list[Receipt]],
    prior_billed: dict[tuple[int, int], float],
    at_day: float,
) -> MatchResult:
    total = float(doc.get("total") or 0.0)
    items = [x for x in doc["lines"] if x["kind"] == "item"]
    charges = [x for x in doc["lines"] if x["kind"] != "item"]
    if doc.get("doc_type") == "credit_memo":
        r = MatchResult("credit_memo", invoice_total=total, payable_amount=total)
        r.summary = (
            f"Credit memo for invoice {doc.get('original_invoice')}. It offsets the next payment to the supplier."
        )
        return r
    if po is None:
        r = MatchResult("non_po", invoice_total=total, payable_amount=0.0, variance_amount=total)
        why = "no PO number on the invoice" if doc.get("po_number") is None else f"PO {doc['po_number']} not found"
        r.issues.append(("non_po", f"Non-PO invoice: {why}. Needs a budget owner's approval to pay."))
        r.summary = f"Non-PO invoice for ${total:,.2f}: {why}."
        return r

    tol = TOLERANCES[po.category]
    mode = "three_way" if tol.three_way else "two_way"
    res = MatchResult(mode, invoice_total=total)
    groups, unmatched = assign_lines(items, po)
    payable = 0.0
    for po_line_no, ils in sorted(groups.items()):
        pl = po.line(po_line_no)
        billed = sum(float(x["qty"] or 0) for x in ils)
        amount = sum(float(x["amount"]) for x in ils)
        inv_price = amount / billed if billed else 0.0
        prior = prior_billed.get((po.id, po_line_no), 0.0)
        rs = [x for x in receipts.get((po.id, po_line_no), []) if x.day <= at_day]
        issues: list[str] = []
        parts = [
            f"PO line {po_line_no} ({pl.item} {pl.description}): billed {fmt_qty(billed)} at ${inv_price:,.4f}"
            + (f" across {len(ils)} invoice lines" if len(ils) > 1 else "")
        ]
        ok_p, pct, var_amt = check_price(inv_price, pl.unit_price, billed, tol)
        pct = 0.0 if abs(pct) < 5e-7 else pct  # no "-0.00%" from float noise
        var_amt = var_amt + 0.0 if abs(var_amt) >= 0.005 else 0.0
        lim = f"limit {tol.price_pct:.0%} and ${tol.price_abs:,.0f}"
        if ok_p:
            parts.append(f"price ${pl.unit_price:,.4f} on PO, variance {pct:+.2%} (${var_amt:,.2f}), within {lim}")
        else:
            issues.append("price")
            parts.append(f"price ${pl.unit_price:,.4f} on PO, variance {pct:+.2%} (${var_amt:,.2f}) exceeds {lim}")
        cap_ordered = pl.qty * (1 + tol.qty_over_pct)
        if tol.three_way:
            received = sum(x.net_qty for x in rs)
            returned = sum(x.qty_returned for x in rs)
            available = min(received, cap_ordered) - prior
            rtxt = f"received {fmt_qty(received)} net"
            if returned:
                rtxt += f" after {fmt_qty(returned)} returned"
            rtxt += f" on {len(rs)} receipt{'s' if len(rs) != 1 else ''}"
            if not rs:
                issues.append("no_receipt")
                parts.append("no goods receipt posted yet")
                available = 0.0
            elif billed > available + EPS:
                issues.append("quantity")
                parts.append(f"{rtxt}, {fmt_qty(prior)} billed before, so {fmt_qty(billed - available)} too many")
            else:
                parts.append(f"{rtxt}, {fmt_qty(prior)} billed before, quantity OK")
        else:
            received = None
            available = cap_ordered - prior
            if billed > available + EPS:
                issues.append("quantity")
                parts.append(
                    f"two-way: ordered {fmt_qty(pl.qty)}, {fmt_qty(prior)} billed before, "
                    f"so {fmt_qty(billed - available)} over the PO"
                )
            else:
                parts.append(f"two-way: ordered {fmt_qty(pl.qty)}, {fmt_qty(prior)} billed before, quantity OK")
        payable_qty = max(0.0, min(billed, available))
        payable += payable_qty * min(inv_price, pl.unit_price)
        res.lines.append(
            LineMatch(
                po_line=po_line_no,
                item=pl.item,
                description=pl.description,
                invoice_lines=[x["line_no"] for x in ils],
                billed_qty=billed,
                invoice_price=round(inv_price, 4),
                po_qty=pl.qty,
                po_price=pl.unit_price,
                received_qty=received,
                receipts=[x.id for x in rs],
                prior_billed=prior,
                price_var_pct=round(pct, 5),
                price_var_amount=var_amt,
                qty_var=round(billed - max(available, 0.0), 4),
                payable_qty=payable_qty,
                status=issues[0] if issues else "ok",
                issues=issues,
                explanation="; ".join(parts) + ".",
            )
        )
    for il in unmatched:
        res.unmatched.append(il)
        res.issues.append(("non_po", f"Invoice line {il['line_no']} ({il.get('description')}) is not on the PO."))

    subtotal = sum(float(x["amount"]) for x in items)
    for c in charges:
        amt = float(c["amount"])
        if c["kind"] == "freight":
            if amt <= FREIGHT_AUTO_ACCEPT:
                cm = ChargeMatch(
                    c["line_no"],
                    "freight",
                    amt,
                    "ok",
                    f"Freight ${amt:,.2f} is under the ${FREIGHT_AUTO_ACCEPT:,.0f} auto-accept limit.",
                )
            else:
                cm = ChargeMatch(
                    c["line_no"],
                    "freight",
                    amt,
                    "non_po",
                    f"Freight ${amt:,.2f} is not on the PO and "
                    f"is over the ${FREIGHT_AUTO_ACCEPT:,.0f} auto-accept limit.",
                )
        else:
            expected = round(subtotal * supplier.tax_rate, 2) if supplier.taxable else 0.0
            if supplier.taxable and abs(amt - expected) <= TAX_ROUNDING:
                cm = ChargeMatch(
                    c["line_no"],
                    "tax",
                    amt,
                    "ok",
                    f"Tax ${amt:,.2f} equals {supplier.tax_rate:.2%} of ${subtotal:,.2f}.",
                )
            else:
                cm = ChargeMatch(
                    c["line_no"],
                    "tax",
                    amt,
                    "non_po",
                    f"Tax ${amt:,.2f} does not match the expected "
                    f"${expected:,.2f}" + ("" if supplier.taxable else " (supplier is not taxable)") + ".",
                )
        res.charges.append(cm)
        if cm.status == "ok":
            payable += amt
        elif c["kind"] == "tax" and supplier.taxable:
            payable += expected
        if cm.status != "ok":
            res.issues.append(("non_po", cm.explanation))

    for t in ("price", "quantity", "no_receipt"):
        bad = [ln for ln in res.lines if t in ln.issues]
        if bad:
            res.issues.append((t, " ".join(issue_text(t, ln, tol) for ln in bad)))
    res.payable_amount = round(payable, 2)
    res.variance_amount = round(total - res.payable_amount, 2)
    n_ok = sum(1 for ln in res.lines if not ln.issues)
    kind = "Three-way" if tol.three_way else "Two-way"
    if res.issues:
        res.summary = (
            f"{kind} match: {n_ok} of {len(res.lines)} PO lines clean; issues: "
            + ", ".join(sorted({t for t, _ in res.issues}))
            + f". Invoice ${total:,.2f}, payable at PO terms ${res.payable_amount:,.2f}."
        )
    else:
        res.summary = f"{kind} match: all {len(res.lines)} PO lines within tolerance. Invoice ${total:,.2f}."
    return res
