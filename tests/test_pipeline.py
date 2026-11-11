import copy

from apmatch.cli import main, markdown
from apmatch.pipeline import gate, kpis


def test_small_run_controls_hold(results):
    c = results.report["controls"]
    assert c["planted_duplicates"] > 0 and c["duplicates_approved"] == 0
    assert c["planted_bank_fraud"] > 0 and c["bank_fraud_approved"] == 0
    assert c["closed_po_approved"] == 0 and c["sod_violations"] == 0


def test_detection_recall_on_planted_problems(results):
    det = results.report["detection"]
    for t in ("price", "quantity", "no_receipt", "duplicate", "bank_change", "closed_po", "non_po"):
        assert det[t]["recall"] == 1.0, t
        assert det[t]["precision"] >= 0.95, t


def test_kpi_math_is_consistent(results):
    k = results.report["kpis"]
    n = k["invoices"]
    assert k["touchless"] / n == round(k["touchless_rate"], 4) or abs(k["touchless"] / n - k["touchless_rate"]) < 1e-4
    assert abs(k["touchless_rate"] + k["exception_rate"] - 1) < 0.01
    assert k["cycle_hours"]["touchless_median"] < k["cycle_hours"]["with_exceptions_median"]
    # Cost per invoice: system cost plus clerk minutes. It must exceed the system cost and be under $20.
    assert 0.85 < k["cost_per_invoice"] < 20
    d = k["discount"]
    assert d["discount_captured"] <= d["discount_available"]
    assert abs(d["capture_rate"] - d["discount_captured"] / d["discount_available"]) < 1e-3


def test_exception_rate_by_type_counts_invoices(results):
    s = results.system
    k = kpis(s, results.as_of, results.schedule)  # live: the API tests may have added invoices
    price_docs = sum(1 for d in s.docs.values() if any(s.exceptions[x]["type"] == "price" for x in d["exceptions"]))
    assert k["exceptions_by_type"]["price"] == price_docs
    assert k["exception_rate_by_type"]["price"] == round(price_docs / k["invoices"], 4)


def test_kpis_on_a_hand_built_set(results):
    s = copy.copy(results.system)
    s.docs = {
        "a": {
            "touchless": True,
            "status": "paid",
            "exceptions": [],
            "touches": [],
            "received_day": 1.0,
            "approved_day": 1.01,
        },
        "b": {
            "touchless": False,
            "status": "paid",
            "exceptions": [],
            "touches": ["price"],
            "received_day": 1.0,
            "approved_day": 3.0,
        },
    }
    s.exceptions = {}
    k = kpis(
        s,
        10.0,
        {
            "cost_of_cash": 0.08,
            "discount_available": 0,
            "discount_captured": 0,
            "discount_missed": 0,
            "discount_declined": 0,
            "capture_rate": None,
            "dpo_days": None,
            "paid_late": 0,
        },
    )
    assert k["touchless_rate"] == 0.5
    # 2 x $0.85 + 22 minutes at $48 an hour, over 2 invoices
    assert k["cost_per_invoice"] == round((2 * 0.85 + 22 / 60 * 48) / 2, 2)
    assert k["cycle_hours"]["with_exceptions_median"] == 48.0


def test_gate_passes_and_fails(results):
    rep = results.report
    assert gate(rep, 0.5) == []
    assert gate(rep, 0.999)
    bad = {**rep, "controls": {**rep["controls"], "duplicates_approved": 1, "bank_fraud_approved": 2}}
    fails = gate(bad, 0.5)
    assert len(fails) == 2 and "duplicate" in fails[0] and "bank" in fails[1]


def test_markdown_summary(results):
    md = markdown(results.report)
    assert "Touchless rate" in md and "Planted duplicates approved | 0 of" in md


def test_cli_run_with_gate(tmp_path, capsys):
    out = tmp_path / "r.json"
    main(
        [
            "run",
            "--pos",
            "600",
            "--suppliers",
            "30",
            "--out",
            str(out),
            "--gate",
            "--min-touchless",
            "0.5",
            "--summary",
            str(tmp_path / "s.md"),
        ]
    )
    assert out.exists() and "gate passed" in capsys.readouterr().out
    assert (tmp_path / "s.md").read_text().startswith("## AP Match results")
