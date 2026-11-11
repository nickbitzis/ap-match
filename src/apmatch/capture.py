"""Invoice capture behind one interface.

`OfflineExtractor` (the default) runs the deterministic parser for the detected layout. `ClaudeExtractor` asks
Claude for the same fields with structured output and a confidence per field. Both return the same dict shape, so
matching never knows which one ran. `evaluate` scores either one against the generator's ground truth per layout."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Protocol

from .config import CAPTURE_CONFIDENCE_FLOOR
from .layouts import detect, parse
from .model import Invoice, to_date

MODEL = "claude-opus-5-5"
HEADER_FIELDS = ["supplier_name", "invoice_number", "invoice_date", "po_number", "routing", "account", "total"]


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> dict: ...


class OfflineExtractor:
    name = "offline parsers"

    def extract(self, text: str) -> dict:
        out = parse(text)
        out["extractor"] = self.name
        return out


def _field(kind: str) -> dict:
    return {
        "type": "object",
        "properties": {"value": {"type": [kind, "null"]}, "confidence": {"type": "number"}},
        "required": ["value", "confidence"],
        "additionalProperties": False,
    }


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ["invoice", "credit_memo"]},
        "supplier_name": _field("string"),
        "invoice_number": _field("string"),
        "invoice_date": _field("string"),
        "po_number": _field("integer"),
        "original_invoice": _field("string"),
        "routing": _field("string"),
        "account": _field("string"),
        "total": _field("number"),
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": ["string", "null"]},
                    "description": {"type": "string"},
                    "qty": {"type": ["number", "null"]},
                    "unit_price": {"type": ["number", "null"]},
                    "amount": {"type": "number"},
                    "kind": {"type": "string", "enum": ["item", "freight", "tax"]},
                    "confidence": {"type": "number"},
                },
                "required": ["item", "description", "qty", "unit_price", "amount", "kind", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["doc_type", *HEADER_FIELDS, "original_invoice", "lines"],
    "additionalProperties": False,
}

SYSTEM = """You capture supplier invoices for an accounts payable team at a manufacturer.
Copy values exactly as printed. Do not fix arithmetic; downstream checks do that.
- invoice_date as YYYY-MM-DD. Slashes are month/day/year. Dots are day.month.year.
- Amounts as plain numbers. In "1.234,50" the comma is the decimal point. Credit memo amounts are negative.
- po_number is the digits of the purchase order number without leading zeros, or null if none is printed.
- routing and account are the remit-to bank details printed on the document.
- Freight and sales tax or VAT are separate lines with kind freight or tax.
- confidence is your probability (0 to 1) that the value is exactly right. Lower it for smudged or ambiguous text,
  for example a letter O where a digit belongs, or a unit price that does not multiply out to the line amount."""


class ExtractionError(RuntimeError):
    pass


class ClaudeExtractor:
    """Claude capture with field-level confidence. Inject `client` in tests; the SDK is imported only when needed."""

    def __init__(self, client=None, model: str = MODEL, effort: str = "low"):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on install extras
                raise RuntimeError("Install the Claude extra first: uv sync --extra claude") from exc
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.name = f"claude ({model})"
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def request(self, text: str) -> dict:
        return {
            "model": self.model,
            "max_tokens": 4000,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": f"<document>\n{text}\n</document>"}],
            "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        }

    def extract(self, text: str) -> dict:
        resp = self.client.messages.create(**self.request(text))
        self.usage["calls"] += 1
        self.usage["input_tokens"] += getattr(resp.usage, "input_tokens", 0)
        self.usage["output_tokens"] += getattr(resp.usage, "output_tokens", 0)
        if resp.stop_reason == "refusal":
            raise ExtractionError("The model declined this document.")
        if resp.stop_reason == "max_tokens":
            raise ExtractionError("The reply was cut off before the JSON was complete.")
        body = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"The reply was not valid JSON: {exc}") from exc
        return from_claude(data, detect(text), self.name)


def from_claude(data: dict, layout: str, name: str) -> dict:
    """Map the structured reply onto the parser's shape. Confidence is clamped to 0..1."""
    out: dict = {"layout": layout, "doc_type": data.get("doc_type", "invoice"), "extractor": name, "confidence": {}}
    for f in [*HEADER_FIELDS, "original_invoice"]:
        cell = data.get(f) or {}
        out[f] = cell.get("value")
        out["confidence"][f] = max(0.0, min(1.0, float(cell.get("confidence", 0.0))))
    if out["po_number"] is not None:
        out["po_number"] = int(out["po_number"])
    out["lines"] = []
    for i, ln in enumerate(data.get("lines", []), start=1):
        conf = max(0.0, min(1.0, float(ln.get("confidence", 0.0))))
        out["lines"].append(
            {
                **{k: ln.get(k) for k in ("item", "description", "qty", "unit_price", "amount", "kind")},
                "line_no": i,
                "confidence": conf,
            }
        )
    out["confidence"]["lines"] = min([x["confidence"] for x in out["lines"]], default=0.0)
    return out


def min_confidence(ext: dict) -> float:
    c = ext["confidence"]
    return min(c.get(f, 0.0) for f in [*HEADER_FIELDS, "lines"])


def low_fields(ext: dict, floor: float = CAPTURE_CONFIDENCE_FLOOR) -> list[str]:
    return [f for f in [*HEADER_FIELDS, "lines"] if ext["confidence"].get(f, 0.0) < floor]


# ---------------------------------------------------------------- evaluation against ground truth


def truth_fields(inv: Invoice, supplier_name: str) -> dict:
    return {
        "supplier_name": supplier_name,
        "invoice_number": inv.number,
        "invoice_date": to_date(inv.invoice_day).isoformat(),
        "po_number": inv.po_id,
        "routing": inv.routing,
        "account": inv.account,
        "total": inv.total,
    }


def _same(a, b) -> bool:
    if isinstance(b, float) or isinstance(a, float):
        try:
            return a is not None and b is not None and abs(float(a) - float(b)) < 0.005
        except (TypeError, ValueError):
            return False
    return a == b


def score(ext: dict, inv: Invoice, supplier_name: str) -> dict[str, bool]:
    """Per field: was the captured value exactly right? `lines` is right only when every line's item, qty, unit
    price, and amount are right and the line count matches."""
    t = truth_fields(inv, supplier_name)
    res = {f: _same(ext.get(f), t[f]) for f in HEADER_FIELDS}
    ok = len(ext["lines"]) == len(inv.lines)
    if ok:
        for got, want in zip(ext["lines"], inv.lines, strict=True):
            if not (
                _same(got["amount"], want.amount)
                and (want.qty is None or (_same(got["qty"], want.qty) and _same(got["unit_price"], want.unit_price)))
                and (want.kind != "item" or want.item is None or got["item"] == want.item)
            ):
                ok = False
                break
    res["lines"] = ok
    return res


def evaluate(invoices: list[Invoice], suppliers: dict, extractor: Extractor | None = None, limit: int | None = None):
    """Field accuracy per layout, the share captured straight through (every field at or above the confidence
    floor), and the share that was confident but wrong (the costly failure: nobody looks at it)."""
    extractor = extractor or OfflineExtractor()
    per = defaultdict(
        lambda: {"n": 0, "fields": defaultdict(int), "straight_through": 0, "confident_wrong": 0, "all": 0}
    )
    for inv in invoices[:limit] if limit else invoices:
        ext = extractor.extract(inv.text)
        res = score(ext, inv, suppliers[inv.supplier_id].name)
        p = per[inv.layout]
        p["n"] += 1
        for f, ok in res.items():
            p["fields"][f] += ok
        p["all"] += all(res.values())
        confident = min_confidence(ext) >= CAPTURE_CONFIDENCE_FLOOR
        p["straight_through"] += confident
        p["confident_wrong"] += confident and not all(res.values())
    out = {}
    for layout, p in sorted(per.items()):
        n = p["n"]
        out[layout] = {
            "invoices": n,
            "field_accuracy": {f: round(v / n, 4) for f, v in p["fields"].items()},
            "all_fields_right": round(p["all"] / n, 4),
            "straight_through": round(p["straight_through"] / n, 4),
            "confident_wrong": round(p["confident_wrong"] / n, 4),
        }
    return {"extractor": extractor.name, "by_layout": out}
