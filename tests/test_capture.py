import json
from types import SimpleNamespace

import pytest

from apmatch.capture import (
    OUTPUT_SCHEMA,
    ClaudeExtractor,
    ExtractionError,
    OfflineExtractor,
    evaluate,
    from_claude,
    low_fields,
    min_confidence,
    score,
)
from apmatch.generator import Config, generate

from .helpers import invoice, supplier


class FakeClient:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload = payload
        self.stop_reason = stop_reason
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=900, output_tokens=210),
        )


def cell(v, c=0.97):
    return {"value": v, "confidence": c}


REPLY = {
    "doc_type": "invoice",
    "supplier_name": cell("Test S0001 Fasteners LLC"),
    "invoice_number": cell("INV-000101"),
    "invoice_date": cell("2025-01-11"),
    "po_number": cell(7),
    "original_invoice": cell(None, 0.99),
    "routing": cell("111000025"),
    "account": cell("1234567890"),
    "total": cell(200.0),
    "lines": [
        {
            "item": "CP-1001",
            "description": "Hex bolt M8x30",
            "qty": 100,
            "unit_price": 2.0,
            "amount": 200.0,
            "kind": "item",
            "confidence": 1.4,
        }
    ],
}


@pytest.fixture(scope="module")
def world():
    return generate(Config(suppliers=40, pos=600, seed=3))


def test_claude_request_shape_and_mapping():
    fake = FakeClient(REPLY)
    out = ClaudeExtractor(client=fake).extract("Test S0001 Fasteners LLC\nINVOICE\n...")
    req = fake.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert "<document>" in req["messages"][0]["content"]
    assert out["invoice_number"] == "INV-000101" and out["po_number"] == 7
    assert out["lines"][0]["confidence"] == 1.0  # clamped
    assert out["confidence"]["invoice_number"] == 0.97


def test_claude_refusal_cutoff_and_bad_json_raise():
    with pytest.raises(ExtractionError):
        ClaudeExtractor(client=FakeClient(REPLY, "refusal")).extract("x")
    with pytest.raises(ExtractionError):
        ClaudeExtractor(client=FakeClient(REPLY, "max_tokens")).extract("x")
    with pytest.raises(ExtractionError):
        ClaudeExtractor(client=FakeClient("not json")).extract("x")


def test_claude_usage_is_counted():
    ex = ClaudeExtractor(client=FakeClient(REPLY))
    ex.extract("a")
    ex.extract("b")
    assert ex.usage == {"input_tokens": 1800, "output_tokens": 420, "calls": 2}


def test_low_confidence_fields_are_listed():
    out = from_claude({**REPLY, "total": cell(200.0, 0.4)}, "block", "claude")
    assert low_fields(out) == ["total"]
    assert min_confidence(out) == 0.4


def test_score_marks_each_field():
    sup = supplier()
    inv = invoice(sup, 7, [("CP-1001", "Hex bolt M8x30", 100, 2.0)])
    ext = OfflineExtractor().extract(inv.text)
    assert all(score(ext, inv, sup.name).values())
    ext["total"] = 201.0
    ext["lines"][0]["qty"] = 99
    res = score(ext, inv, sup.name)
    assert res["total"] is False and res["lines"] is False and res["invoice_number"] is True


def test_field_accuracy_per_layout(world):
    rep = evaluate(world.invoices, world.suppliers)
    by = rep["by_layout"]
    assert set(by) == {"block", "pipe", "euro", "letter"}
    for layout in ("block", "pipe", "euro"):
        assert by[layout]["all_fields_right"] >= 0.99
    # Scanned letters with a dropped decimal fail the arithmetic check and are routed to a clerk, not trusted.
    assert by["letter"]["straight_through"] < 1.0
    assert all(v["confident_wrong"] <= 0.01 for v in by.values())


def test_evaluate_with_a_fake_claude_extractor(world):
    rep = evaluate(world.invoices, world.suppliers, ClaudeExtractor(client=FakeClient(REPLY)), limit=5)
    assert sum(v["invoices"] for v in rep["by_layout"].values()) == 5
    assert rep["extractor"].startswith("claude")
