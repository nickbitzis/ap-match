"""Four supplier invoice layouts: a renderer for each (used by the generator) and a deterministic parser for each
(used by capture). Parsers return plain dicts with a confidence per field.

block   fixed-width table, ISO dates, US amounts
pipe    EDI-like key|value records, MM/DD/YYYY dates, plain amounts
euro    DD.MM.YYYY dates and 1.234,56 amounts
letter  free text from a small supplier, sometimes scanned (OCR noise in amounts)
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .model import Invoice, Supplier, to_date

LAYOUTS = ["block", "pipe", "euro", "letter"]
BILL_TO = "Kestrel Ridge Fabrication, Accounts Payable, 400 Foundry Road, Millbrook"
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


# ---------------------------------------------------------------- number helpers


def us(x: float) -> str:
    return f"{x:,.2f}"


def eu(x: float) -> str:
    return f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def price_txt(p: float, sep: str = ".") -> str:
    """Unit prices keep up to 4 decimals (small parts are priced in fractions of a cent), at least 2."""
    whole, frac = f"{p:.4f}".split(".")
    frac = frac.rstrip("0").ljust(2, "0")
    return f"{int(whole):,}".replace(",", "." if sep == "," else ",") + sep + frac


def qty_txt(q: float) -> str:
    return f"{q:g}" if q == int(q) else f"{q:.2f}"


def parse_us(s: str) -> tuple[float, bool]:
    """Parse '1,234.50'. Returns (value, repaired) where repaired means OCR letters were mapped to digits."""
    s = s.strip().replace("$", "")
    repaired = bool(re.search(r"[OoIl]", s))
    s = s.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")
    return float(s.replace(",", "")), repaired


def parse_eu(s: str) -> float:
    return float(s.strip().replace(".", "").replace(",", "."))


# ---------------------------------------------------------------- renderers


def render(inv: Invoice, sup: Supplier, noise: dict | None = None) -> str:
    return {"block": render_block, "pipe": render_pipe, "euro": render_euro, "letter": render_letter}[inv.layout](
        inv, sup, noise or {}
    )


def _doc(inv: Invoice) -> str:
    return "CREDIT MEMO" if inv.is_credit else "INVOICE"


def render_block(inv: Invoice, sup: Supplier, noise: dict) -> str:
    d = to_date(inv.invoice_day).isoformat()
    out = [
        sup.name,
        "1200 Commerce Way",
        _doc(inv),
        f"{'Credit Memo' if inv.is_credit else 'Invoice'} Number: {inv.number}",
    ]
    out.append(f"Invoice Date: {d}")
    if inv.po_id is not None:
        out.append(f"PO Number: PO-{inv.po_id:07d}")
    if inv.original_number:
        out.append(f"Original Invoice: {inv.original_number}")
    out += [
        f"Bill To: {BILL_TO}",
        f"Remit To: Bank routing {inv.routing} account {inv.account}",
        f"Terms: {sup.terms}",
        "",
    ]
    out.append(f"{'ITEM':<12}{'DESCRIPTION':<40}{'QTY':>10}{'UNIT PRICE':>14}{'AMOUNT':>16}")
    for ln in inv.lines:
        if ln.kind == "item" and ln.qty is not None:
            cols = f"{ln.item or '-':<12}{ln.description:<40}{qty_txt(ln.qty):>10}"
            out.append(f"{cols}{ln.unit_price:>14.4f}{us(ln.amount):>16}")
        else:
            tag = {"freight": "FREIGHT", "tax": "TAX"}.get(ln.kind, "CREDIT")
            out.append(f"{tag:<12}{ln.description:<40}{'':>10}{'':>14}{us(ln.amount):>16}")
    out.append(f"{'':<52}{'TOTAL':>24}{us(inv.total):>16}")
    return "\n".join(out) + "\n"


def render_pipe(inv: Invoice, sup: Supplier, noise: dict) -> str:
    d = to_date(inv.invoice_day)
    out = [f"DOC|{'CREDIT' if inv.is_credit else 'INVOICE'}", f"SUPPLIER|{sup.name}", f"INV_NO|{inv.number}"]
    out.append(f"INV_DATE|{d.month:02d}/{d.day:02d}/{d.year}")
    if inv.po_id is not None:
        out.append(f"PO|{inv.po_id:07d}")
    if inv.original_number:
        out.append(f"ORIG_INV|{inv.original_number}")
    out.append(f"REMIT|{inv.routing}|{inv.account}")
    for ln in inv.lines:
        if ln.kind == "item" and ln.qty is not None:
            out.append(
                f"LINE|{ln.line_no}|{ln.item}|{ln.description}|{qty_txt(ln.qty)}|{ln.unit_price:.4f}|{ln.amount:.2f}"
            )
        else:
            out.append(f"CHARGE|{ln.kind.upper()}|{ln.description}|{ln.amount:.2f}")
    out.append(f"TOTAL|{inv.total:.2f}")
    return "\n".join(out) + "\n"


def render_euro(inv: Invoice, sup: Supplier, noise: dict) -> str:
    d = to_date(inv.invoice_day)
    out = [sup.name, "Industriestrasse 14", "Credit note" if inv.is_credit else "Invoice"]
    out.append(f"Invoice no.: {inv.number}    Date: {d.day:02d}.{d.month:02d}.{d.year}")
    if inv.po_id is not None:
        out.append(f"Your order: PO-{inv.po_id:07d}")
    if inv.original_number:
        out.append(f"Refers to invoice: {inv.original_number}")
    out.append(f"Bank: routing {inv.routing} / account {inv.account}")
    out.append("Pos | Article | Description | Qty | Price | Amount")
    for ln in inv.lines:
        if ln.kind == "item" and ln.qty is not None:
            price = price_txt(ln.unit_price, ",")
            out.append(f"{ln.line_no} | {ln.item} | {ln.description} | {eu(ln.qty)} | {price} | {eu(ln.amount)}")
        else:
            label = {"freight": "Freight", "tax": "VAT"}.get(ln.kind, "Credit")
            out.append(f"{label}: {eu(ln.amount)}")
    out.append(f"Total due: {eu(inv.total)}")
    return "\n".join(out) + "\n"


def _ocr(s: str, on: bool) -> str:
    # A scanned letter sometimes reads a zero as the letter O.
    return s.replace("0", "O", 1) if on and "0" in s else s


def render_letter(inv: Invoice, sup: Supplier, noise: dict) -> str:
    d = to_date(inv.invoice_day)
    when = f"{MONTHS[d.month - 1]} {d.day}, {d.year}"
    kind = "credit memo" if inv.is_credit else "invoice"
    out = [f"From {sup.name}", "To: Accounts Payable, Kestrel Ridge Fabrication", ""]
    head = f"Please find our {kind} {inv.number} dated {when}"
    if inv.po_id is not None:
        head += f" against your purchase order {inv.po_id}"
    if inv.original_number:
        head += f" for invoice {inv.original_number}"
    out.append(head + ".")
    out.append(f"Kindly remit to routing {inv.routing}, account {inv.account}.")
    out.append("")
    for i, ln in enumerate(inv.lines):
        if ln.kind == "item" and ln.qty is not None:
            price = price_txt(ln.unit_price)
            if noise.get("drop_decimal") == i:
                price = price.replace(".", "")
            amt = _ocr(us(ln.amount), noise.get("ocr_o") == i)
            out.append(f"  {qty_txt(ln.qty)} x {ln.description} ({ln.item}) at ${price} each = ${amt}")
        else:
            label = {"freight": "Freight", "tax": "Sales tax"}.get(ln.kind, "Credit")
            out.append(f"  {label}: ${us(ln.amount)}")
    out.append(f"Amount due: ${us(inv.total)}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- parsers


def detect(text: str) -> str:
    t = text.lstrip()
    if t.startswith("DOC|"):
        return "pipe"
    if "Please find our" in text:
        return "letter"
    if "Invoice no.:" in text:
        return "euro"
    return "block"


def _empty(layout: str) -> dict:
    return {
        "layout": layout,
        "doc_type": "invoice",
        "supplier_name": None,
        "invoice_number": None,
        "invoice_date": None,
        "po_number": None,
        "original_invoice": None,
        "routing": None,
        "account": None,
        "total": None,
        "lines": [],
        "confidence": {},
    }


def _finish(out: dict) -> dict:
    """Arithmetic checks set line and total confidence: qty x price must equal the line amount, and the lines must
    add up to the total. A field the parser never found gets confidence 0."""
    c = out["confidence"]
    for f in ("supplier_name", "invoice_number", "invoice_date", "routing", "account", "total"):
        if out[f] is None:
            c[f] = 0.0
        else:
            c.setdefault(f, 1.0)
    c.setdefault("po_number", 1.0 if out["po_number"] is not None else 0.9)  # a missing PO is a fact, not a miss
    for ln in out["lines"]:
        conf = ln.pop("_conf", 1.0)
        if ln["kind"] == "item" and ln["qty"] is not None and ln["unit_price"] is not None:
            if abs(ln["qty"] * ln["unit_price"] - ln["amount"]) > 0.011 + 0.0001 * abs(ln["amount"]):
                conf = min(conf, 0.3)
        ln["confidence"] = conf
    if out["total"] is not None and out["lines"]:
        if abs(sum(x["amount"] for x in out["lines"]) - out["total"]) > 0.011:
            c["total"] = min(c["total"], 0.5)
    c["lines"] = min([x["confidence"] for x in out["lines"]], default=0.0)
    return out


def _line(no, item, desc, qty, price, amount, kind="item", conf=1.0) -> dict:
    return {
        "line_no": no,
        "item": item,
        "description": desc,
        "qty": qty,
        "unit_price": price,
        "amount": amount,
        "kind": kind,
        "_conf": conf,
    }


def parse_block(text: str) -> dict:
    out = _empty("block")
    rows = text.splitlines()
    out["supplier_name"] = rows[0].strip() if rows else None
    if "CREDIT MEMO" in text:
        out["doc_type"] = "credit_memo"
    if m := re.search(r"(?:Invoice|Credit Memo) Number:[ \t]*(.+?)[ \t]*$", text, re.M):
        out["invoice_number"] = m.group(1)
    if m := re.search(r"Invoice Date:\s*(\d{4}-\d{2}-\d{2})", text):
        out["invoice_date"] = m.group(1)
    if m := re.search(r"PO Number:\s*PO-(\d+)", text):
        out["po_number"] = int(m.group(1))
    if m := re.search(r"Original Invoice:[ \t]*(.+?)[ \t]*$", text, re.M):
        out["original_invoice"] = m.group(1)
    if m := re.search(r"routing (\d{9}) account (\d+)", text):
        out["routing"], out["account"] = m.group(1), m.group(2)
    n = 0
    for r in rows:
        m = re.match(r"^(\S+)\s{2,}(.+?)\s{2,}(-?[\d.,]+)\s{2,}(-?[\d.,]+)\s{2,}(-?[\d,]+\.\d{2})$", r)
        if m:
            n += 1
            out["lines"].append(
                _line(n, m.group(1), m.group(2), float(m.group(3)), float(m.group(4)), parse_us(m.group(5))[0])
            )
            continue
        m = re.match(r"^(FREIGHT|TAX|CREDIT)\s{2,}(.+?)\s{2,}(-?[\d,]+\.\d{2})$", r)
        if m:
            n += 1
            kind = {"FREIGHT": "freight", "TAX": "tax", "CREDIT": "item"}[m.group(1)]
            out["lines"].append(_line(n, None, m.group(2), None, None, parse_us(m.group(3))[0], kind))
    if m := re.search(r"TOTAL\s+(-?[\d,]+\.\d{2})", text):
        out["total"] = parse_us(m.group(1))[0]
    return _finish(out)


def parse_pipe(text: str) -> dict:
    out = _empty("pipe")
    n = 0
    for r in text.splitlines():
        f = r.split("|")
        k = f[0]
        if k == "DOC" and len(f) > 1 and f[1] == "CREDIT":
            out["doc_type"] = "credit_memo"
        elif k == "SUPPLIER":
            out["supplier_name"] = f[1]
        elif k == "INV_NO":
            out["invoice_number"] = f[1]
        elif k == "INV_DATE":
            mm, dd, yy = f[1].split("/")
            out["invoice_date"] = f"{yy}-{mm}-{dd}"
        elif k == "PO":
            out["po_number"] = int(f[1])
        elif k == "ORIG_INV":
            out["original_invoice"] = f[1]
        elif k == "REMIT":
            out["routing"], out["account"] = f[1], f[2]
        elif k == "LINE" and len(f) == 7:
            n += 1
            out["lines"].append(_line(n, f[2], f[3], float(f[4]), float(f[5]), float(f[6])))
        elif k == "CHARGE":
            n += 1
            kind = f[1].lower() if f[1] in ("FREIGHT", "TAX") else "item"
            out["lines"].append(_line(n, None, f[2], None, None, float(f[3]), kind))
        elif k == "TOTAL":
            out["total"] = float(f[1])
    return _finish(out)


def parse_euro(text: str) -> dict:
    out = _empty("euro")
    rows = text.splitlines()
    out["supplier_name"] = rows[0].strip() if rows else None
    if "Credit note" in text:
        out["doc_type"] = "credit_memo"
    if m := re.search(r"Invoice no\.:[ \t]*(.+?)[ \t]{2,}Date:\s*(\d{2})\.(\d{2})\.(\d{4})", text):
        out["invoice_number"] = m.group(1)
        out["invoice_date"] = f"{m.group(4)}-{m.group(3)}-{m.group(2)}"
    if m := re.search(r"Your order:\s*PO-(\d+)", text):
        out["po_number"] = int(m.group(1))
    if m := re.search(r"Refers to invoice:[ \t]*(.+?)[ \t]*$", text, re.M):
        out["original_invoice"] = m.group(1)
    if m := re.search(r"routing (\d{9}) / account (\d+)", text):
        out["routing"], out["account"] = m.group(1), m.group(2)
    n = 0
    for r in rows:
        f = [x.strip() for x in r.split("|")]
        if len(f) == 6 and f[0].isdigit():
            n += 1
            out["lines"].append(_line(n, f[1], f[2], parse_eu(f[3]), parse_eu(f[4]), parse_eu(f[5])))
            continue
        m = re.match(r"^(Freight|VAT|Credit):\s*(-?[\d.,]+)$", r)
        if m:
            n += 1
            kind = {"Freight": "freight", "VAT": "tax", "Credit": "item"}[m.group(1)]
            out["lines"].append(_line(n, None, m.group(1), None, None, parse_eu(m.group(2)), kind))
    if m := re.search(r"Total due:\s*(-?[\d.,]+)", text):
        out["total"] = parse_eu(m.group(1))
    return _finish(out)


_MONEY = r"-?[\dOoIl,]+\.[\dOoIl]{2}"


def parse_letter(text: str) -> dict:
    out = _empty("letter")
    rows = text.splitlines()
    if rows and rows[0].startswith("From "):
        out["supplier_name"] = rows[0][5:].strip()
    m = re.search(r"Please find our (invoice|credit memo) (.+?) dated (\w+) (\d{1,2}), (\d{4})", text)
    if m:
        out["doc_type"] = "credit_memo" if m.group(1) == "credit memo" else "invoice"
        out["invoice_number"] = m.group(2)
        if m.group(3) in MONTHS:
            out["invoice_date"] = date(int(m.group(5)), MONTHS.index(m.group(3)) + 1, int(m.group(4))).isoformat()
    if m := re.search(r"against your purchase order (\d+)", text):
        out["po_number"] = int(m.group(1))
    if m := re.search(r"for invoice (.+?)\.?$", text, re.M):
        out["original_invoice"] = m.group(1)
    if m := re.search(r"routing (\d{9}), account (\d+)", text):
        out["routing"], out["account"] = m.group(1), m.group(2)
    n = 0
    for r in rows:
        m = re.match(rf"^\s+([\d.]+) x (.+) \((\S+)\) at \$([\d,]+(?:\.\d+)?) each = \$({_MONEY})$", r)
        if m:
            n += 1
            amt, rep = parse_us(m.group(5))
            out["lines"].append(
                _line(
                    n,
                    m.group(3),
                    m.group(2),
                    float(m.group(1)),
                    float(m.group(4).replace(",", "")),
                    amt,
                    conf=0.9 if rep else 1.0,
                )
            )
            continue
        m = re.match(rf"^\s+(Freight|Sales tax|Credit): \$({_MONEY})$", r)
        if m:
            n += 1
            kind = {"Freight": "freight", "Sales tax": "tax", "Credit": "item"}[m.group(1)]
            out["lines"].append(_line(n, None, m.group(1), None, None, parse_us(m.group(2))[0], kind))
    if m := re.search(rf"Amount due: \$({_MONEY})", text):
        out["total"] = parse_us(m.group(1))[0]
    return _finish(out)


PARSERS = {"block": parse_block, "pipe": parse_pipe, "euro": parse_euro, "letter": parse_letter}


def parse(text: str, layout: str | None = None) -> dict:
    layout = layout or detect(text)
    try:
        return PARSERS[layout](text)
    except (ValueError, IndexError) as exc:  # a mangled document becomes an empty, zero-confidence capture
        out = _finish(_empty(layout))
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


def iso_to_day(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float((datetime.fromisoformat(s) - datetime(2025, 1, 1)).days)
    except ValueError:
        return None
