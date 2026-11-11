"""FastAPI service: ingest invoices, match results with explanations, the exceptions queue and its actions,
payment run proposals, KPIs, the audit log, /healthz, /metrics, and the web UI."""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .capture import ClaudeExtractor, ExtractionError
from .config import ALLOWED_ACTIONS, TERMS
from .controls import SoDViolation
from .engine import ActionError
from .layouts import iso_to_day
from .logs import get_logger, log_event
from .metrics import Metrics
from .model import to_date, to_iso
from .payments import annualized_return, plan, proposal
from .pipeline import Config, Results, controls, kpis, run, series

STATIC = Path(__file__).parent / "static"


class IngestBody(BaseModel):
    text: str = Field(min_length=10, max_length=200_000)
    received_on: str | None = None  # YYYY-MM-DD, defaults to the system's current day


class ActionBody(BaseModel):
    action: str
    actor: str
    note: str = ""
    fields: dict | None = None


def extractor_from_env():
    if os.environ.get("APMATCH_EXTRACTOR", "offline") == "claude":
        return ClaudeExtractor()
    return None


def create_app(results: Results | None = None, cfg: Config | None = None, data_dir: str | None = None) -> FastAPI:
    log = get_logger()
    metrics = Metrics()
    lock = threading.RLock()
    state: dict = {"r": results, "ready": results is not None}
    data_dir = data_dir or os.environ.get("APMATCH_DATA_DIR", "data")

    def res() -> Results:
        with lock:
            if state["r"] is None:
                t = time.perf_counter()
                pos = int(os.environ.get("APMATCH_POS", "25000"))
                c = cfg or Config(pos=pos, suppliers=max(40, int(400 * pos / 25000)))
                state["r"] = run(c)
                # The simulated year is captured offline. A Claude extractor, if configured, handles live ingest only.
                if (x := extractor_from_env()) is not None:
                    state["r"].system.extractor = x
                state["ready"] = True
                log_event(log, "pipeline_done", seconds=round(time.perf_counter() - t, 1), pos=pos)
            if "now" not in state:
                state["now"] = state["r"].as_of + 0.5  # noon on the as-of day
                # The simulated year stays in memory. Live actions from here on are also appended to disk.
                state["r"].system.audit_path = Path(data_dir) / "audit.jsonl"
                publish()
            return state["r"]

    def tick() -> float:
        state["now"] += 1 / 1440  # each API action advances the clock one minute so the audit log stays ordered
        return state["now"]

    def replan() -> None:
        r = state["r"]
        r.schedule = plan(list(r.system.docs.values()), r.system.suppliers, r.as_of)

    def publish() -> None:
        r = state["r"]
        k = kpis(r.system, r.as_of, r.schedule)
        metrics.set_gauge(
            "apmatch_touchless_rate", "Invoices approved with no human touch, share.", k["touchless_rate"]
        )
        metrics.set_gauge("apmatch_cost_per_invoice_usd", "Modeled cost per invoice.", k["cost_per_invoice"])
        metrics.set_gauge("apmatch_dpo_days", "Amount-weighted days from invoice to payment.", k["dpo_days"] or 0)
        metrics.set_gauge(
            "apmatch_discount_captured_usd", "Early payment discounts captured.", k["discount"]["discount_captured"]
        )
        metrics.set_gauge(
            "apmatch_discount_missed_usd", "Discounts worth taking that were missed.", k["discount"]["discount_missed"]
        )
        for t, v in k["sla"].items():
            metrics.set_gauge("apmatch_exceptions_open", "Open exceptions by type.", v["open"], type=t)
        for s, n in k["status"].items():
            metrics.set_gauge("apmatch_invoices", "Invoices by status.", n, status=s)
        c = controls(r.system)
        metrics.set_gauge(
            "apmatch_duplicates_approved", "Planted duplicates approved (must be 0).", c["duplicates_approved"]
        )
        metrics.set_gauge(
            "apmatch_bank_fraud_approved", "Planted bank fraud approved (must be 0).", c["bank_fraud_approved"]
        )

    @asynccontextmanager
    async def lifespan(_app):
        if os.environ.get("APMATCH_WARM", "1") == "1":
            res()
        yield

    app = FastAPI(title="AP Match", version="1.0.0", lifespan=lifespan)
    if results is not None:
        res()

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        response = await call_next(request)
        dur = time.perf_counter() - start
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        metrics.observe_request(path, response.status_code, dur)
        response.headers["x-request-id"] = rid
        if path not in ("/healthz", "/metrics"):
            log_event(
                log,
                "request",
                request_id=rid,
                method=request.method,
                path=path,
                status=response.status_code,
                ms=round(dur * 1000, 1),
            )
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok" if state["ready"] else "starting", "ready": state["ready"]}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom():
        return metrics.render()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ------------------------------------------------------------ summary and KPIs

    @app.get("/api/summary")
    def summary():
        r = res()
        rep = r.report
        return {
            "company": rep["company"],
            "as_of": rep["as_of"],
            "now": to_iso(state["now"]),
            "counts": rep["counts"],
            "kpis": kpis(r.system, r.as_of, r.schedule),
            "series": series(r.system),
            "detection": rep["detection"],
            "controls": controls(r.system),
            "capture": rep["capture"],
            "terms": rep["terms"],
            "extractor": r.system.extractor.name,
        }

    @app.get("/api/people")
    def people():
        return [vars(p) for p in res().system.people.values()]

    # ------------------------------------------------------------ invoices

    def doc_view(doc: dict) -> dict:
        r = res()
        s = r.system
        sup = s.suppliers.get(doc.get("supplier_id") or "")
        po = s.pos.get(doc.get("po_id")) if doc.get("po_id") is not None else None
        receipts = []
        if po:
            for ln in po.lines:
                # Only receipts posted by the system clock; later ones are still in the future.
                receipts += [vars(x) for x in s.receipts.get((po.id, ln.line_no), []) if x.day <= state["now"]]
        return {
            **{k: v for k, v in doc.items() if k not in ("truth", "history", "text")},
            "received_at": to_iso(doc["received_day"]),
            "invoice_date": to_date(doc["invoice_day"]).isoformat() if doc.get("invoice_day") is not None else None,
            "approved_at": to_iso(doc.get("approved_day")),
            "supplier": {
                "id": sup.id,
                "name": sup.name,
                "terms": sup.terms,
                "routing": sup.routing,
                "account": sup.account,
                "category": sup.category,
            }
            if sup
            else None,
            "po": {
                "number": po.number,
                "status": po.status,
                "created_by": po.created_by,
                "category": po.category,
                "lines": [{**vars(x), "amount": x.amount} for x in po.lines],
                "total": po.total,
            }
            if po
            else None,
            "receipts": receipts,
            "exceptions": [s.exceptions[x] for x in doc["exceptions"]],
            "text": doc.get("text"),
            "planted": (doc.get("truth") or {}).get("planted", []),
        }

    @app.post("/api/invoices", status_code=201)
    def ingest(body: IngestBody):
        r = res()
        with lock:
            at = tick()
            if body.received_on:
                day = iso_to_day(body.received_on)
                if day is None:
                    raise HTTPException(400, "received_on must be YYYY-MM-DD")
                at = max(at, day + 0.5)
            try:
                doc = r.system.ingest(body.text, at, channel="api")
            except ExtractionError as exc:
                raise HTTPException(422, str(exc)) from exc
            replan()
            publish()
        log_event(
            log,
            "invoice_ingested",
            doc=doc["id"],
            status=doc["status"],
            exceptions=[r.system.exceptions[x]["type"] for x in doc["exceptions"]],
        )
        return doc_view(doc)

    @app.get("/api/invoices")
    def invoices(status: str | None = None, supplier: str | None = None, limit: int = Query(50, ge=1, le=1000)):
        docs = list(res().system.docs.values())
        if status:
            docs = [d for d in docs if d["status"] == status]
        if supplier:
            docs = [d for d in docs if d.get("supplier_id") == supplier]
        docs = sorted(docs, key=lambda d: -d["received_day"])[:limit]
        return [
            {
                "id": d["id"],
                "number": d.get("number"),
                "supplier_id": d.get("supplier_id"),
                "total": d.get("total"),
                "status": d["status"],
                "received_at": to_iso(d["received_day"]),
                "exceptions": len(d["exceptions"]),
            }
            for d in docs
        ]

    @app.get("/api/invoices/{doc_id}")
    def invoice(doc_id: str):
        doc = res().system.docs.get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no invoice {doc_id}")
        return doc_view(doc)

    @app.get("/api/invoices/{doc_id}/match")
    def invoice_match(doc_id: str):
        doc = res().system.docs.get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no invoice {doc_id}")
        return doc.get("match") or {"mode": None, "summary": "Not matched yet: waiting on capture review."}

    # ------------------------------------------------------------ exceptions

    @app.get("/api/exceptions")
    def exception_queue(
        type: str | None = None,
        owner: str | None = None,
        status: str = Query("open", pattern="^(open|resolved|closed|all)$"),
        overdue: bool | None = None,
        limit: int = Query(200, ge=1, le=5000),
    ):
        r = res()
        s = r.system
        rows = s.queue(type, owner, status, now=state["now"])
        if overdue is not None:
            rows = [e for e in rows if e.get("overdue") == overdue]
        out = []
        for e in rows[:limit]:
            d = s.docs[e["doc_id"]]
            sup = s.suppliers.get(d.get("supplier_id") or "")
            out.append(
                {
                    **{k: v for k, v in e.items() if k != "history"},
                    "created_at": to_iso(e["created_day"]),
                    "due_at": to_iso(e["due_day"]),
                    "supplier": sup.name if sup else d.get("supplier_name"),
                    "invoice_number": d.get("number"),
                    "amount": d.get("total"),
                }
            )
        counts: dict[str, int] = {}
        for e in s.queue(None, None, "open"):
            counts[e["type"]] = counts.get(e["type"], 0) + 1
        return {"count": len(rows), "open_by_type": counts, "rows": out}

    @app.get("/api/exceptions/{exc_id}")
    def exception(exc_id: str):
        s = res().system
        e = s.exceptions.get(exc_id)
        if e is None:
            raise HTTPException(404, f"no exception {exc_id}")
        out = {
            **e,
            "allowed_actions": sorted(ALLOWED_ACTIONS[e["type"]]),
            "po_creator": s.po_creator(s.docs[e["doc_id"]]),
        }
        if "accept_variance" in ALLOWED_ACTIONS[e["type"]] and e["status"] == "open":
            try:
                out["approval_route"] = s.approval_route(e)
            except SoDViolation as exc:
                out["approval_route"] = {"error": str(exc)}
        return out

    @app.post("/api/exceptions/{exc_id}/actions")
    def act(exc_id: str, body: ActionBody):
        r = res()
        s = r.system
        with lock:
            try:
                e = s.act(exc_id, body.action, body.actor, tick(), body.note, body.fields)
            except KeyError as exc:
                raise HTTPException(404, f"no exception {exc_id}") from exc
            except SoDViolation as exc:
                raise HTTPException(403, str(exc)) from exc
            except ActionError as exc:
                raise HTTPException(400, str(exc)) from exc
            replan()
            publish()
        metrics.action(e["type"], body.action)
        log_event(log, "exception_action", exception=exc_id, action=body.action, actor=body.actor)
        doc = s.docs[e["doc_id"]]
        return {"exception": e, "invoice_status": doc["status"], "payment": doc.get("payment")}

    # ------------------------------------------------------------ payments

    @app.get("/api/payments/plan")
    def payment_plan(cost_of_cash: float = Query(0.08, ge=0, le=1)):
        """What-if: re-plan the whole year at a different cost of cash. Works on copies, so nothing changes."""
        r = res()
        copies = [{**d} for d in r.system.docs.values()]
        p = plan(copies, r.system.suppliers, r.as_of, cost_of_cash)
        upcoming = [x for x in p["runs"] if x["status"] == "proposed"]
        terms = [
            {"terms": k, "annualized_return": round(v, 4), "take": v > cost_of_cash}
            for k, v in ((t, annualized_return(TERMS[t])) for t in TERMS)
        ]
        return {
            **{k: v for k, v in p.items() if k != "runs"},
            "upcoming_runs": upcoming[:12],
            "recent_runs": [x for x in p["runs"] if x["status"] == "paid"][-8:],
            "terms": terms,
        }

    @app.get("/api/payments/proposal")
    def payment_proposal(run_date: str | None = None):
        r = res()
        upcoming = [x for x in r.schedule["runs"] if x["status"] == "proposed"]
        if run_date is None:
            if not upcoming:
                raise HTTPException(404, "no proposed runs")
            day = upcoming[0]["day"]
        else:
            try:
                day = int(iso_to_day(date.fromisoformat(run_date).isoformat()))
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, "run_date must be YYYY-MM-DD") from exc
        return {
            **proposal(list(r.system.docs.values()), r.system.suppliers, day),
            "proposed_runs": [x["date"] for x in upcoming],
        }

    # ------------------------------------------------------------ audit

    @app.get("/api/audit")
    def audit(doc: str | None = None, limit: int = Query(100, ge=1, le=5000)):
        rows = res().system.audit
        if doc:
            rows = [x for x in rows if x.get("doc") == doc]
        return {"count": len(rows), "rows": rows[-limit:][::-1]}

    return app


def serve(host: str | None = None, port: int | None = None) -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(
        create_app(),
        host=host or os.environ.get("HOST", "127.0.0.1"),
        port=int(port or os.environ.get("PORT", "8887")),
        log_level="warning",
    )
