"""The AP back office: ingest -> capture -> match -> controls -> exceptions -> approval, plus exception actions,
the bank change workflow, and an audit log. The simulator and the API both drive this one class."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from .capture import Extractor, OfflineExtractor, low_fields
from .config import ALLOWED_ACTIONS, CAPTURE_CONFIDENCE_FLOOR, OWNERS, SLA_HOURS
from .controls import DuplicateIndex, SoDViolation, bank_matches, check_sod, eligible_roles, route_approval
from .layouts import iso_to_day
from .matching import match
from .model import Person, PurchaseOrder, Receipt, Supplier, to_iso

APPROVER_ROLES = {"ap_supervisor", "purchasing_manager", "controller", "cfo"}
ACTOR_ROLES = {
    "capture": {"ap_clerk", "ap_supervisor"},
    "price": {"buyer", "ap_clerk", "ap_supervisor", "purchasing_manager"},
    "quantity": {"receiving", "ap_clerk", "ap_supervisor", "buyer"},
    "no_receipt": {"receiving", "ap_clerk", "ap_supervisor"},
    "duplicate": {"ap_clerk", "ap_supervisor"},
    "bank_change": {"treasury", "controller"},
    "non_po": {"ap_clerk", "ap_supervisor", "buyer"},
    "closed_po": {"buyer", "purchasing_manager", "ap_clerk"},
}


class ActionError(ValueError):
    pass


class APSystem:
    def __init__(
        self,
        suppliers: dict[str, Supplier],
        people: dict[str, Person],
        pos: dict[int, PurchaseOrder],
        receipts: list[Receipt],
        extractor: Extractor | None = None,
        audit_path: str | Path | None = None,
    ):
        self.suppliers = suppliers
        self.people = people
        self.pos = pos
        self.receipts: dict[tuple[int, int], list[Receipt]] = defaultdict(list)
        for r in receipts:
            self.receipts[(r.po_id, r.line_no)].append(r)
        self.extractor = extractor or OfflineExtractor()
        self.by_name = {s.name.lower(): s.id for s in suppliers.values()}
        self.docs: dict[str, dict] = {}
        self.exceptions: dict[str, dict] = {}
        self.billed: dict[tuple[int, int], float] = defaultdict(float)
        self.dups = DuplicateIndex()
        self.audit: list[dict] = []
        self.audit_path = Path(audit_path) if audit_path else None
        self.seq = 0
        self.exc_seq = 0

    # ------------------------------------------------------------ audit

    def log(self, at: float, actor: str, action: str, **fields) -> None:
        entry = {"at": to_iso(at), "actor": actor, "action": action, **fields}
        self.audit.append(entry)
        if self.audit_path:
            try:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.audit_path, "a") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except OSError:  # a read-only disk should not stop processing; the in-memory log still has it
                self.audit_path = None

    # ------------------------------------------------------------ ingest

    def ingest(
        self,
        text: str,
        received_day: float,
        doc_id: str | None = None,
        truth: dict | None = None,
        channel: str = "email",
    ) -> dict:
        self.seq += 1
        doc_id = doc_id or f"D{self.seq:06d}"
        ext = self.extractor.extract(text)
        doc = {
            "id": doc_id,
            "text": text,
            "channel": channel,
            "layout": ext["layout"],
            "extractor": ext.get("extractor", ""),
            "capture_confidence": ext["confidence"],
            "received_day": received_day,
            "exceptions": [],
            "status": "received",
            "touches": [],
            "approved_day": None,
            "approved_amount": None,
            "truth": truth or {},
            "history": [],
        }
        self._apply_fields(doc, ext)
        self.docs[doc_id] = doc
        self.log(received_day, "system", "ingest", doc=doc_id, layout=doc["layout"], extractor=doc["extractor"])
        low = low_fields(ext, CAPTURE_CONFIDENCE_FLOOR)
        if doc["supplier_id"] is None:
            low = sorted(set(low) | {"supplier_name"})
        if low:
            doc["status"] = "exception"
            self._raise(
                doc,
                "capture",
                f"Low capture confidence on: {', '.join(low)}. Key these fields from the document image.",
                received_day,
            )
            return doc
        self._evaluate(doc, received_day)
        return doc

    def _apply_fields(self, doc: dict, ext: dict) -> None:
        name = (ext.get("supplier_name") or "").lower()
        doc.update(
            {
                "supplier_id": self.by_name.get(name),
                "supplier_name": ext.get("supplier_name"),
                "number": ext.get("invoice_number"),
                "invoice_day": iso_to_day(ext.get("invoice_date")),
                "po_number": ext.get("po_number"),
                "po_id": ext.get("po_number") if ext.get("po_number") in self.pos else None,
                "original_invoice": ext.get("original_invoice"),
                "routing": ext.get("routing"),
                "account": ext.get("account"),
                "total": ext.get("total"),
                "doc_type": ext.get("doc_type", "invoice"),
                "lines": [dict(x) for x in ext.get("lines", [])],
            }
        )
        if doc["invoice_day"] is None:
            doc["invoice_day"] = float(int(doc["received_day"]))

    def _covered(self, doc: dict, m) -> Callable[[dict], bool]:
        def covered(prev: dict) -> bool:
            if any(t in ("quantity", "no_receipt") for t, _ in m.issues) or m.mode != "three_way":
                return False
            po_id = doc.get("po_id")
            for ln in m.lines:
                for r in self.receipts.get((po_id, ln.po_line), []):
                    if prev["day"] < r.day <= (doc["invoice_day"] or 0) + 0.999:
                        return True
            return False

        return covered

    def _evaluate(self, doc: dict, at: float) -> None:
        """Match and run controls. Raises exceptions for every problem found; approves if there are none."""
        sup = self.suppliers[doc["supplier_id"]]
        po = self.pos.get(doc["po_id"]) if doc["po_id"] is not None else None
        m = match(doc, po, sup, self.receipts, self.billed, at)
        doc["match"] = m.to_dict()
        found: list[tuple[str, str]] = []
        # Duplicates first: a duplicate is held and nothing else about it matters.
        hit = self.dups.check(doc, self._covered(doc, m))
        if hit:
            doc["duplicate_of"] = hit.of
            found.append(("duplicate", hit.detail))
        elif po is not None and po.status == "closed" and (po.closed_day or 0) <= at:
            found.append(("closed_po", f"{po.number} was closed on {to_iso(po.closed_day)[:10]}."))
        else:
            if po is not None and po.supplier_id != doc["supplier_id"]:
                found.append(("non_po", f"{po.number} belongs to a different supplier."))
            if not bank_matches(doc, sup.routing, sup.account):
                found.append(
                    (
                        "bank_change",
                        f"Remit-to bank {doc['routing']}/{doc['account']} differs from the "
                        f"master {sup.routing}/{sup.account}. Hold until treasury verifies by calling the "
                        "number on file.",
                    )
                )
            if doc["doc_type"] == "credit_memo" and not self._find_original(doc):
                found.append(
                    ("non_po", f"Credit memo refers to invoice {doc.get('original_invoice')!r}, which is not on file.")
                )
            found += m.issues
            arith = sum(float(x["amount"]) for x in doc["lines"])
            if doc["total"] is not None and abs(arith - doc["total"]) > 0.011:
                found.append(("capture", f"Lines add up to ${arith:,.2f} but the total says ${doc['total']:,.2f}."))
        if not doc.get("duplicate_of"):
            self.dups.add(doc)
            self._bill(doc, m, sign=1)
        doc["status"] = "exception" if found else "matching"
        seen = set()
        for t, detail in found:
            key = (t, detail)
            if key not in seen:
                seen.add(key)
                self._raise(doc, t, detail, at)
        self._maybe_approve(doc, at)

    def _find_original(self, doc: dict) -> dict | None:
        prev = self.dups.find(doc["supplier_id"], doc.get("original_invoice"))
        return self.docs.get(prev["id"]) if prev else None

    def _bill(self, doc: dict, m, sign: int, qty_override: dict | None = None) -> None:
        if m.mode not in ("three_way", "two_way") or doc.get("po_id") is None:
            return
        for ln in m.lines:
            key = (doc["po_id"], ln.po_line)
            q = qty_override.get(ln.po_line, ln.billed_qty) if qty_override else ln.billed_qty
            self.billed[key] += sign * q
        doc["billed"] = {ln.po_line: (qty_override or {}).get(ln.po_line, ln.billed_qty) for ln in m.lines}

    def _unbill(self, doc: dict) -> None:
        for line_no, q in (doc.pop("billed", None) or {}).items():
            self.billed[(doc["po_id"], line_no)] -= q

    def _raise(self, doc: dict, etype: str, detail: str, at: float) -> dict:
        self.exc_seq += 1
        e = {
            "id": f"E{self.exc_seq:06d}",
            "doc_id": doc["id"],
            "type": etype,
            "owner": OWNERS[etype],
            "detail": detail,
            "status": "open",
            "created_day": at,
            "sla_hours": SLA_HOURS[etype],
            "due_day": at + SLA_HOURS[etype] / 24,
            "resolution": None,
            "resolved_by": None,
            "resolved_day": None,
            "pending": None,  # e.g. waiting for a second approver or a credit memo
            "history": [],
        }
        self.exceptions[e["id"]] = e
        doc["exceptions"].append(e["id"])
        self.log(at, "system", "exception_raised", doc=doc["id"], exception=e["id"], type=etype)
        return e

    def open_exceptions(self, doc: dict) -> list[dict]:
        return [self.exceptions[x] for x in doc["exceptions"] if self.exceptions[x]["status"] == "open"]

    def _maybe_approve(self, doc: dict, at: float) -> None:
        if doc["status"] in ("rejected", "approved", "scheduled", "paid") or self.open_exceptions(doc):
            return
        decided = [self.exceptions[x] for x in doc["exceptions"]]
        amount = doc["total"]
        for e in decided:
            if e["resolution"] == "short_pay":
                amount = doc["match"]["payable_amount"]
        doc["status"] = "approved"
        doc["approved_day"] = at if decided else at + 0.01  # touchless: about 15 minutes of machine time
        doc["approved_amount"] = round(float(amount or 0), 2)
        doc["touchless"] = not decided
        self.log(
            doc["approved_day"],
            "system",
            "approved",
            doc=doc["id"],
            amount=doc["approved_amount"],
            touchless=doc["touchless"],
        )

    # ------------------------------------------------------------ actions

    def po_creator(self, doc: dict) -> str | None:
        po = self.pos.get(doc.get("po_id")) if doc.get("po_id") is not None else None
        return po.created_by if po else None

    def approval_route(self, e: dict) -> dict:
        doc = self.docs[e["doc_id"]]
        variance = self.variance(doc, e)
        exclude = {x for x in [self.po_creator(doc), (e.get("pending") or {}).get("requested_by")] if x}
        return route_approval(variance, float(doc["total"] or 0), self.people, exclude, key=e["id"])

    def variance(self, doc: dict, e: dict) -> float:
        if e["type"] in ("non_po", "closed_po") and doc.get("po_id") is None:
            return float(doc["total"] or 0)
        return max(0.0, float(doc["match"]["variance_amount"])) if doc.get("match") else float(doc["total"] or 0)

    def act(self, exc_id: str, action: str, actor: str, at: float, note: str = "", fields: dict | None = None) -> dict:
        e = self.exceptions.get(exc_id)
        if e is None:
            raise KeyError(exc_id)
        if e["status"] != "open":
            raise ActionError(f"{exc_id} is already {e['status']}")
        if action not in ALLOWED_ACTIONS[e["type"]] | {"credit_memo_received"}:
            raise ActionError(f"{action} is not allowed for a {e['type']} exception")
        person = self.people.get(actor)
        if person is None:
            raise ActionError(f"unknown person {actor}")
        doc = self.docs[e["doc_id"]]
        if action == "accept_variance":
            # Segregation of duties first, so the PO creator always hears why, whatever their approval limit.
            check_sod(actor, action, self.po_creator(doc), (e.get("pending") or {}).get("requested_by"))
            route = self.approval_route(e)
            if person.role not in eligible_roles(self.variance(doc, e), float(doc["total"] or 0)):
                raise ActionError(
                    f"{actor} ({person.role}) cannot approve this variance; route: {', '.join(route['path'])}"
                )
        elif action == "approve_bank_change":
            if person.role not in {"treasury", "controller"}:
                raise ActionError("only treasury or the controller approves a bank change")
            pend = e.get("pending") or {}
            if pend.get("step") != "callback_confirmed":
                raise ActionError("a callback must confirm the change before it can be approved")
            check_sod(actor, action, None, pend.get("requested_by"))
        elif person.role not in ACTOR_ROLES[e["type"]] | APPROVER_ROLES:
            raise ActionError(f"{actor} ({person.role}) does not work {e['type']} exceptions")
        e["history"].append({"at": to_iso(at), "actor": actor, "action": action, "note": note})
        self.log(at, actor, action, doc=doc["id"], exception=exc_id, note=note)
        if action not in ("hold",):
            doc["touches"].append(e["type"])

        if action == "hold":
            e["pending"] = {"step": "hold", "requested_by": actor}
            doc["status"] = "on_hold"
            return e
        if action == "request_credit_memo":
            e["pending"] = {"step": "awaiting_credit_memo", "requested_by": actor}
            doc["status"] = "awaiting_credit_memo"
            return e
        if action == "callback_confirmed":
            e["pending"] = {"step": "callback_confirmed", "requested_by": actor}
            return e
        if action == "approve_bank_change":
            sup = self.suppliers[doc["supplier_id"]]
            sup.bank_history.append((at, sup.routing, sup.account, e["pending"]["requested_by"], actor))
            sup.routing, sup.account = doc["routing"], doc["account"]
            self.log(at, actor, "bank_master_updated", supplier=sup.id, routing=sup.routing)
            self._resolve(e, doc, "bank_change_verified", actor, at)
            self._maybe_approve(doc, at)
            return e
        if action == "callback_failed":
            self._resolve(e, doc, "bank_change_rejected", actor, at)
            self._reject(doc, actor, at, "Supplier did not confirm the bank change. Possible fraud.")
            return e
        if action == "reject":
            self._resolve(e, doc, "reject", actor, at)
            self._reject(doc, actor, at, note or f"Rejected from a {e['type']} exception")
            return e
        if action == "not_duplicate":
            doc.pop("duplicate_of", None)
            self._resolve(e, doc, "not_duplicate", actor, at)
            m = match(
                doc,
                self.pos.get(doc["po_id"]) if doc["po_id"] else None,
                self.suppliers[doc["supplier_id"]],
                self.receipts,
                self.billed,
                at,
            )
            doc["match"] = m.to_dict()
            self.dups.add(doc)
            self._bill(doc, m, 1)
            for t, detail in m.issues:
                self._raise(doc, t, detail, at)
            self._maybe_approve(doc, at)
            return e
        if action == "correct_fields":
            # The clerk keys the header and lines from the image. Re-run everything on the corrected fields.
            ext = {**fields} if fields else {}
            ext.setdefault("confidence", {})
            self._apply_fields(doc, ext)
            self._resolve(e, doc, "correct_fields", actor, at)
            if doc["supplier_id"] is None:
                self._reject(doc, actor, at, "Supplier not in the vendor master")
                return e
            self._evaluate(doc, at)
            return e
        if action == "receipt_posted":
            m = match(
                doc,
                self.pos.get(doc["po_id"]),
                self.suppliers[doc["supplier_id"]],
                self.receipts,
                self._prior_without(doc),
                at,
            )
            if any(t in ("no_receipt",) for t, _ in m.issues):
                e["history"].append({"at": to_iso(at), "actor": "system", "action": "still_no_receipt"})
                raise ActionError("no receipt is posted for this PO yet")
            doc["match"] = m.to_dict()
            self._resolve(e, doc, "receipt_posted", actor, at)
            for t, detail in m.issues:
                if t not in {x["type"] for x in self.open_exceptions(doc)} and t != "no_receipt":
                    self._raise(doc, t, detail, at)
            self._maybe_approve(doc, at)
            return e
        if action == "short_pay":
            m = doc.get("match") or {}
            self._unbill(doc)
            self._bill_short(doc, m)
            self._resolve(e, doc, "short_pay", actor, at)
            # Short pay settles every line issue at once: price and quantity are both paid at PO terms.
            for other in self.open_exceptions(doc):
                if other["type"] in ("price", "quantity", "non_po") and other["id"] != e["id"]:
                    self._resolve(other, doc, "short_pay", actor, at)
            self._maybe_approve(doc, at)
            return e
        if action in ("accept_variance", "credit_memo_received"):
            self._resolve(e, doc, action, actor, at)
            self._maybe_approve(doc, at)
            return e
        raise ActionError(f"unhandled action {action}")

    def _prior_without(self, doc: dict) -> dict:
        """Billed quantities on this document's PO, minus what this document itself billed."""
        po = self.pos[doc["po_id"]]
        own = doc.get("billed") or {}
        return {
            (po.id, ln.line_no): self.billed.get((po.id, ln.line_no), 0.0) - own.get(ln.line_no, 0.0) for ln in po.lines
        }

    def _bill_short(self, doc: dict, m: dict) -> None:
        if doc.get("po_id") is None:
            return
        doc["billed"] = {}
        for ln in m.get("lines", []):
            self.billed[(doc["po_id"], ln["po_line"])] += ln["payable_qty"]
            doc["billed"][ln["po_line"]] = ln["payable_qty"]

    def _resolve(self, e: dict, doc: dict, resolution: str, actor: str, at: float) -> dict:
        e.update({"status": "resolved", "resolution": resolution, "resolved_by": actor, "resolved_day": at})
        e["pending"] = None
        if doc["status"] in ("on_hold", "awaiting_credit_memo"):
            doc["status"] = "exception"
        return e

    def _reject(self, doc: dict, actor: str, at: float, reason: str) -> dict:
        for other in self.open_exceptions(doc):
            other.update(
                {"status": "closed", "resolution": "invoice_rejected", "resolved_by": actor, "resolved_day": at}
            )
        self._unbill(doc)
        if doc.get("supplier_id") and doc.get("duplicate_of") is None:
            self.dups.remove(doc)
        doc["status"] = "rejected"
        doc["rejected_reason"] = reason
        self.log(at, actor, "invoice_rejected", doc=doc["id"], reason=reason)
        return doc

    # ------------------------------------------------------------ views

    def queue(
        self, etype: str | None = None, owner: str | None = None, status: str = "open", now: float | None = None
    ) -> list[dict]:
        out = []
        for e in self.exceptions.values():
            if status != "all" and e["status"] != status:
                continue
            if etype and e["type"] != etype:
                continue
            if owner and e["owner"] != owner:
                continue
            out.append(e)
        if now is not None:
            for e in out:
                e["hours_left"] = round((e["due_day"] - now) * 24, 1)
                e["overdue"] = e["status"] == "open" and now > e["due_day"]
        return sorted(out, key=lambda x: x["due_day"])


SoDViolation = SoDViolation  # re-exported for the API
