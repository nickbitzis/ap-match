import pytest
from fastapi.testclient import TestClient

from apmatch.app import create_app

from .helpers import invoice


@pytest.fixture(scope="module")
def client(results, tmp_path_factory):
    return TestClient(create_app(results=results, data_dir=str(tmp_path_factory.mktemp("data"))))


def test_healthz_metrics_and_index(client):
    assert client.get("/healthz").json() == {"status": "ok", "ready": True}
    client.get("/api/summary")
    m = client.get("/metrics").text
    assert "apmatch_touchless_rate" in m and "apmatch_duplicates_approved 0" in m
    assert 'apmatch_http_requests_total{route="/api/summary",status="200"}' in m
    assert "AP Match" in client.get("/").text


def test_summary_shape(client):
    s = client.get("/api/summary").json()
    assert {"kpis", "series", "detection", "controls", "capture"} <= set(s)
    assert s["series"]["weekly"] and s["kpis"]["invoices"] > 1000


def test_invoice_detail_has_three_way_view(client, results):
    doc_id = next(d["id"] for d in results.system.docs.values() if d.get("match", {}).get("mode") == "three_way")
    d = client.get(f"/api/invoices/{doc_id}").json()
    assert d["po"]["lines"] and d["receipts"] and d["match"]["lines"][0]["explanation"]
    assert client.get(f"/api/invoices/{doc_id}/match").json()["mode"] == "three_way"
    assert client.get("/api/invoices/NOPE").status_code == 404
    assert client.get("/api/invoices", params={"status": "rejected", "limit": 5}).json()


def test_ingest_clean_then_duplicate(client, results):
    src = next(d for d in results.system.docs.values() if d.get("touchless") and d["status"] == "paid")
    r = client.post("/api/invoices", json={"text": src["text"]})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "exception" and [e["type"] for e in body["exceptions"]] == ["duplicate"]
    assert client.post("/api/invoices", json={"text": "short"}).status_code == 422


def test_exception_queue_filters(client):
    q = client.get("/api/exceptions", params={"status": "all", "type": "price", "limit": 20}).json()
    assert q["rows"] and all(e["type"] == "price" for e in q["rows"])
    q = client.get("/api/exceptions", params={"owner": "receiving", "status": "all"}).json()
    assert all(e["owner"] == "receiving" for e in q["rows"])
    assert client.get("/api/exceptions", params={"status": "bogus"}).status_code == 422


def test_actions_enforce_sod_and_roles(client):
    q = client.get("/api/exceptions", params={"type": "duplicate"}).json()
    e = q["rows"][0]
    info = client.get(f"/api/exceptions/{e['id']}").json()
    assert "reject" in info["allowed_actions"]
    bad = client.post(f"/api/exceptions/{e['id']}/actions", json={"action": "short_pay", "actor": "A01"})
    assert bad.status_code == 400
    ok = client.post(f"/api/exceptions/{e['id']}/actions", json={"action": "reject", "actor": "A01", "note": "dup"})
    assert ok.status_code == 200 and ok.json()["invoice_status"] == "rejected"
    assert (
        client.post(f"/api/exceptions/{e['id']}/actions", json={"action": "reject", "actor": "A01"}).status_code == 400
    )
    assert client.post("/api/exceptions/E0/actions", json={"action": "hold", "actor": "A01"}).status_code == 404
    audit = client.get("/api/audit", params={"limit": 5}).json()["rows"]
    assert audit[0]["action"] in ("reject", "invoice_rejected")


def test_sod_violation_is_403(client, results):
    s = results.system
    po = next(p for p in s.pos.values() if p.category != "services" and p.status == "open")
    sup = s.suppliers[po.supplier_id]
    ln = po.lines[0]
    inv = invoice(
        sup,
        po.id,
        [(ln.item, ln.description, 1, round(ln.unit_price * 1.5, 2))],
        number="SOD-TEST-1",
        day=results.as_of,
    )
    body = client.post("/api/invoices", json={"text": inv.text}).json()
    exc = next(e for e in body["exceptions"] if e["type"] == "price")
    r = client.post(f"/api/exceptions/{exc['id']}/actions", json={"action": "accept_variance", "actor": po.created_by})
    assert r.status_code == 403 and "created the PO" in r.json()["detail"]


def test_payment_plan_and_proposal(client):
    p = client.get("/api/payments/plan").json()
    assert p["upcoming_runs"] and {t["terms"]: t["take"] for t in p["terms"]}["2/10 net 30"] is True
    hi = client.get("/api/payments/plan", params={"cost_of_cash": 0.5}).json()
    assert hi["discount_captured"] == 0 and hi["dpo_days"] > p["dpo_days"]
    prop = client.get("/api/payments/proposal").json()
    assert prop["payments"] and prop["total"] == round(sum(x["net"] for x in prop["payments"]), 2)
    run_date = p["upcoming_runs"][1]["date"]
    assert client.get("/api/payments/proposal", params={"run_date": run_date}).json()["run_date"] == run_date
    assert client.get("/api/payments/proposal", params={"run_date": "soon"}).status_code == 400


def test_people(client):
    roles = {p["role"] for p in client.get("/api/people").json()}
    assert {"buyer", "receiving", "ap_clerk", "treasury", "controller"} <= roles
