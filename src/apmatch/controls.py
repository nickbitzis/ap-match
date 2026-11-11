"""Fraud and control checks: duplicate invoices, remit-to bank changes, segregation of duties, approval routing."""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from .config import APPROVAL_LEVELS, DUPLICATE_WINDOW_DAYS, LARGE_VARIANCE_PCT
from .model import Person


def normalize_number(num: str | None) -> str:
    """'INV-004512', 'inv 4512', '#INV4512' all become '|4512'. '4512-K' and 'K-00004512' both become 'K|4512'.
    Letters other than an INV prefix are kept; leading zeros are dropped. OCR letter O in digits reads as zero."""
    s = (num or "").upper()
    s = re.sub(r"(?<=\d)O|O(?=\d)", "0", s)
    letters = "".join(ch for ch in s if ch.isalpha())
    if letters.startswith("INV"):
        letters = letters[3:]
    digits = "".join(ch for ch in s if ch.isdigit()).lstrip("0")
    return f"{letters}|{digits}"


def near(a: str, b: str) -> bool:
    """One substitution or one swap of adjacent characters (a re-keyed number)."""
    if a == b or len(a) != len(b):
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 1:
        return True
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


@dataclass
class DupHit:
    of: str
    rule: str  # same_number | fuzzy_number | same_content
    detail: str


class DuplicateIndex:
    """Every invoice that was not rejected as a duplicate, indexed by supplier and normalized number (exact rule)
    and by supplier and amount (fuzzy and same-content rules), so a check touches a handful of candidates."""

    def __init__(self):
        self.by_norm: dict[tuple[str, str], list[dict]] = {}
        self.by_amount: dict[tuple[str, float], list[dict]] = {}

    @staticmethod
    def entry(doc: dict) -> dict:
        return {
            "id": doc["id"],
            "number": doc.get("number") or "",
            "norm": normalize_number(doc.get("number")),
            "total": round(float(doc.get("total") or 0), 2),
            "day": doc.get("invoice_day") or 0.0,
            "po": doc.get("po_id"),
            "items": tuple(
                sorted((x.get("item") or "", x.get("qty") or 0) for x in doc["lines"] if x["kind"] == "item")
            ),
            "credit": doc.get("doc_type") == "credit_memo",
        }

    def add(self, doc: dict) -> None:
        e = self.entry(doc)
        self.by_norm.setdefault((doc["supplier_id"], e["norm"]), []).append(e)
        self.by_amount.setdefault((doc["supplier_id"], e["total"]), []).append(e)

    def remove(self, doc: dict) -> None:
        e = self.entry(doc)
        for idx, k in (
            (self.by_norm, (doc["supplier_id"], e["norm"])),
            (self.by_amount, (doc["supplier_id"], e["total"])),
        ):
            if k in idx:
                idx[k] = [x for x in idx[k] if x["id"] != doc["id"]]

    def find(self, supplier_id: str, number: str | None, credit: bool = False) -> dict | None:
        for prev in self.by_norm.get((supplier_id, normalize_number(number)), []):
            if prev["credit"] == credit:
                return prev
        return None

    def check(self, doc: dict, covered: Callable[[dict], bool] = lambda prev: False) -> DupHit | None:
        """`covered(prev)` says whether a real second delivery explains this invoice: receipts cover it on top of
        everything billed before, and goods arrived after the earlier invoice was dated. Two invoices with similar
        numbers and the same amount are then two real shipments (a hard negative), not a duplicate."""
        me = self.entry(doc)
        sup = doc["supplier_id"]
        if me["norm"] != "|":
            for prev in self.by_norm.get((sup, me["norm"]), []):
                if prev["id"] != me["id"] and prev["credit"] == me["credit"]:
                    return DupHit(
                        prev["id"],
                        "same_number",
                        f"Invoice number {me['number']!r} matches "
                        f"{prev['number']!r} on {prev['id']} after normalizing.",
                    )
        for prev in self.by_amount.get((sup, me["total"]), []):
            if prev["id"] == me["id"] or prev["credit"] != me["credit"]:
                continue
            if abs(prev["day"] - me["day"]) > DUPLICATE_WINDOW_DAYS:
                continue
            if near(prev["norm"], me["norm"]) and not covered(prev):
                return DupHit(
                    prev["id"],
                    "fuzzy_number",
                    f"Same supplier and amount ${me['total']:,.2f} within "
                    f"{DUPLICATE_WINDOW_DAYS} days, and number {me['number']!r} is one keystroke from "
                    f"{prev['number']!r} on {prev['id']}. No new delivery explains a second invoice.",
                )
            if prev["po"] == me["po"] and prev["items"] == me["items"] and me["items"] and not covered(prev):
                return DupHit(
                    prev["id"],
                    "same_content",
                    f"Same supplier, PO, lines, and amount ${me['total']:,.2f} "
                    f"as {prev['id']} within {DUPLICATE_WINDOW_DAYS} days. No new delivery explains a "
                    "second invoice.",
                )
        return None


def bank_matches(doc: dict, routing: str, account: str) -> bool:
    return doc.get("routing") == routing and doc.get("account") == account


class SoDViolation(PermissionError):
    pass


def check_sod(actor: str, action: str, po_creator: str | None, requested_by: str | None = None) -> None:
    """The person who created the PO cannot approve a variance on its invoice, and nobody approves their own
    request (maker-checker). Bank change approval needs a different person from the one who made the callback."""
    if action in ("accept_variance", "approve_bank_change") and po_creator and actor == po_creator:
        raise SoDViolation(f"{actor} created the PO and cannot approve this invoice's variance.")
    if action in ("accept_variance", "approve_bank_change") and requested_by and actor == requested_by:
        raise SoDViolation(f"{actor} made the request and cannot also approve it.")


def required_level(variance: float, invoice_total: float) -> int:
    """Index into APPROVAL_LEVELS: the first level whose limits cover the variance and the invoice, plus one if
    the variance is more than 10% of the invoice."""
    v, a = abs(variance), abs(invoice_total)
    idx = next(i for i, lv in enumerate(APPROVAL_LEVELS) if v <= lv.max_variance and a <= lv.max_invoice)
    if a > 0 and v / a > LARGE_VARIANCE_PCT:
        idx = min(idx + 1, len(APPROVAL_LEVELS) - 1)
    return idx


def route_approval(
    variance: float, invoice_total: float, people: dict[str, Person], exclude: set[str], key: str = ""
) -> dict:
    """Pick an approver at the required level who is not excluded by segregation of duties. If nobody at that
    level is eligible, escalate. `key` spreads work across eligible people deterministically."""
    start = required_level(variance, invoice_total)
    path = []
    for idx in range(start, len(APPROVAL_LEVELS)):
        role = APPROVAL_LEVELS[idx].role
        pool = sorted(p.id for p in people.values() if p.role == role and p.id not in exclude)
        if pool:
            pick = pool[zlib.crc32(key.encode()) % len(pool)]
            path.append(f"{role}: {pick}")
            return {
                "role": role,
                "approver": pick,
                "level": idx,
                "required_level": start,
                "path": path,
                "escalated": idx > start,
            }
        path.append(f"{role}: nobody eligible (segregation of duties), escalate")
    raise SoDViolation("no eligible approver")


def eligible_roles(variance: float, invoice_total: float) -> set[str]:
    start = required_level(variance, invoice_total)
    return {lv.role for lv in APPROVAL_LEVELS[start:]}
