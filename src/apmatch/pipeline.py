"""Run a year of AP through the engine as a discrete-event simulation, then measure it.

Invoices arrive at their received time. Each exception gets a simulated human decision after a delay drawn per
type, by a person in the right role. The simulated people follow a written policy and never see the planted labels,
with two exceptions that stand in for the outside world: a supplier answering a bank change callback, and a supplier
confirming whether an invoice was sent twice. Events after the as-of date stay unprocessed, which leaves a live
exceptions queue for the UI and API."""

from __future__ import annotations

import copy
import heapq
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .capture import Extractor, evaluate
from .config import COST, COST_OF_CASH, EXCEPTION_TYPES, TERMS
from .engine import ActionError, APSystem
from .generator import Config as GenConfig
from .generator import World, generate
from .model import Invoice, to_date
from .payments import annualized_return, plan

# Median hours from an exception being raised to a person acting on it. Assumptions, spread with a lognormal.
MEDIAN_HOURS = {
    "capture": 5,
    "price": 40,
    "quantity": 36,
    "no_receipt": 48,
    "duplicate": 10,
    "bank_change": 20,
    "non_po": 30,
    "closed_po": 36,
}
APPROVAL_WAIT_HOURS = 6
APPROVED = ("approved", "scheduled", "paid")
NEVER_RECEIVED_REJECT_DAYS = 30


@dataclass
class Config:
    suppliers: int = 400
    pos: int = 25_000
    seed: int = 42
    capture_sample: int | None = None  # None scores capture on every invoice
    cost_of_cash: float = COST_OF_CASH
    audit_path: str | None = None
    # Mid-December by default: invoices received after this have not arrived yet, and decisions after it have not
    # been made, so the exceptions queue has a realistic backlog.
    as_of_day: float | None = 349.0


@dataclass
class Results:
    world: World
    system: APSystem
    as_of: float
    report: dict
    schedule: dict = field(default_factory=dict)


def truth_fields(inv: Invoice, world: World) -> dict:
    """What a clerk keys from the document image."""
    return {
        "layout": inv.layout,
        "doc_type": "credit_memo" if inv.is_credit else "invoice",
        "supplier_name": world.suppliers[inv.supplier_id].name,
        "invoice_number": inv.number,
        "invoice_date": to_date(inv.invoice_day).isoformat(),
        "po_number": inv.po_id,
        "original_invoice": inv.original_number,
        "routing": inv.routing,
        "account": inv.account,
        "total": inv.total,
        "lines": [
            {
                "line_no": x.line_no,
                "item": x.item,
                "description": x.description,
                "qty": x.qty,
                "unit_price": x.unit_price,
                "amount": x.amount,
                "kind": x.kind,
                "confidence": 1.0,
            }
            for x in inv.lines
        ],
        "confidence": {},
    }


class Simulator:
    def __init__(self, world: World, system: APSystem, seed: int):
        self.w = world
        self.sys = system
        self.rng = random.Random(seed + 7)
        self.events: list = []
        self.n = 0
        self.planned: set[str] = set()
        self.by_role: dict[str, list[str]] = defaultdict(list)
        for p in world.people.values():
            self.by_role[p.role].append(p.id)
        self.invoices = {i.id: i for i in world.invoices}

    def push(self, t: float, kind: str, payload) -> None:
        self.n += 1
        heapq.heappush(self.events, (t, self.n, kind, payload))

    def delay(self, etype: str) -> float:
        return self.rng.lognormvariate(math.log(MEDIAN_HOURS[etype]), 0.6) / 24

    def who(self, role: str, exclude: set[str] | None = None) -> str:
        pool = [p for p in self.by_role[role] if p not in (exclude or set())]
        return self.rng.choice(pool)

    def run(self, as_of: float) -> None:
        for inv in self.w.invoices:
            self.push(inv.received_day, "ingest", inv)
        while self.events and self.events[0][0] <= as_of:
            t, _, kind, payload = heapq.heappop(self.events)
            if kind == "ingest":
                doc = self.sys.ingest(payload.text, t, doc_id=payload.id, truth=payload.truth, channel=payload.channel)
            else:
                exc_id, action, actor, extra = payload
                e = self.sys.exceptions[exc_id]
                doc = self.sys.docs[e["doc_id"]]
                if e["status"] != "open":
                    continue
                try:
                    self.sys.act(exc_id, action, actor, t, note="simulated", fields=extra.get("fields"))
                except ActionError:
                    if action == "receipt_posted":
                        self.planned.discard(exc_id)
                if extra.get("then"):
                    self.push(t + extra["then"][0], "act", (exc_id, *extra["then"][1:]))
            self.plan(doc, t)

    def plan(self, doc: dict, t: float) -> None:
        for e in self.sys.open_exceptions(doc):
            if e["id"] in self.planned:
                continue
            self.planned.add(e["id"])
            self.policy(doc, e, t)

    def policy(self, doc: dict, e: dict, t: float) -> None:
        inv = self.invoices.get(doc["id"])
        truth = doc.get("truth") or {}
        planted = set(truth.get("planted", []))
        d = t + self.delay(e["type"])
        typ = e["type"]
        if typ == "capture":
            self.push(
                d, "act", (e["id"], "correct_fields", self.who("ap_clerk"), {"fields": truth_fields(inv, self.w)})
            )
        elif typ == "duplicate":
            real = bool(planted & {"duplicate", "duplicate_fuzzy"})
            self.push(d, "act", (e["id"], "reject" if real else "not_duplicate", self.who("ap_clerk"), {}))
        elif typ == "bank_change":
            caller = self.who("treasury")
            if "bank_fraud" in planted:
                self.push(d, "act", (e["id"], "callback_failed", caller, {}))
            else:
                approver = self.who("treasury", {caller})
                self.push(
                    d,
                    "act",
                    (
                        e["id"],
                        "callback_confirmed",
                        caller,
                        {"then": (APPROVAL_WAIT_HOURS / 24, "approve_bank_change", approver, {})},
                    ),
                )
        elif typ == "closed_po":
            self.push(d, "act", (e["id"], "reject", self.sys.po_creator(doc) or self.who("buyer"), {}))
        elif typ == "no_receipt":
            po = self.sys.pos.get(doc["po_id"])
            days = [r.day for ln in po.lines for r in self.sys.receipts.get((po.id, ln.line_no), [])] if po else []
            if days:
                self.push(max(d, max(days) + 0.1), "act", (e["id"], "receipt_posted", self.who("receiving"), {}))
            else:
                self.push(
                    d,
                    "act",
                    (
                        e["id"],
                        "hold",
                        self.who("receiving"),
                        {"then": (NEVER_RECEIVED_REJECT_DAYS, "reject", self.who("ap_clerk"), {})},
                    ),
                )
        elif typ in ("price", "quantity", "non_po"):
            m = doc.get("match") or {}
            variance = max(0.0, float(m.get("variance_amount", 0.0)))
            worst = max([abs(x["price_var_pct"]) for x in m.get("lines", []) if "price" in x["issues"]], default=0.0)
            accept = (
                (typ == "price" and worst <= 0.05)
                or (typ == "non_po" and doc.get("po_id") is None)
                or (typ == "non_po" and variance <= 250)
            )
            if accept:
                route = self.sys.approval_route(e)
                self.push(d + APPROVAL_WAIT_HOURS / 24, "act", (e["id"], "accept_variance", route["approver"], {}))
            else:
                role = {"price": "ap_clerk", "quantity": "receiving", "non_po": "ap_clerk"}[typ]
                self.push(d, "act", (e["id"], "short_pay", self.who(role), {}))


def run(cfg: Config | None = None, extractor: Extractor | None = None) -> Results:
    cfg = cfg or Config()
    world = generate(GenConfig(suppliers=cfg.suppliers, pos=cfg.pos, seed=cfg.seed))
    # The vendor master is copied so the bank change workflow can update it without touching the generator's view.
    suppliers = copy.deepcopy(world.suppliers)
    system = APSystem(suppliers, world.people, world.pos, world.receipts, extractor, cfg.audit_path)
    as_of = cfg.as_of_day if cfg.as_of_day is not None else float(int(max(i.received_day for i in world.invoices)))
    sim = Simulator(world, system, cfg.seed)
    sim.run(as_of)
    schedule = plan(list(system.docs.values()), system.suppliers, as_of, cfg.cost_of_cash)
    report = build_report(world, system, as_of, schedule, cfg, extractor)
    return Results(world, system, as_of, report, schedule)


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1)


def kpis(system: APSystem, as_of: float, schedule: dict) -> dict:
    docs = list(system.docs.values())
    n = len(docs)
    touchless = sum(1 for d in docs if d.get("touchless") and d["status"] in ("approved", "scheduled", "paid"))
    by_type = Counter()
    for d in docs:
        for t in {system.exceptions[x]["type"] for x in d["exceptions"]}:
            by_type[t] += 1
    cyc_all, cyc_tl, cyc_ex = [], [], []
    for d in docs:
        if d.get("approved_day") is None:
            continue
        h = (d["approved_day"] - d["received_day"]) * 24
        cyc_all.append(h)
        (cyc_tl if d.get("touchless") else cyc_ex).append(h)
    minutes = 0.0
    for d in docs:
        minutes += sum(COST.touch_minutes[t] for t in d["touches"])
        decided = [system.exceptions[x] for x in d["exceptions"]]
        minutes += COST.touch_minutes["approval"] * sum(1 for e in decided if e["resolution"] == "accept_variance")
    cost = (COST.system_per_invoice * n + minutes / 60 * COST.loaded_rate_per_hour) / n if n else 0.0
    sla = {}
    for t in EXCEPTION_TYPES:
        es = [e for e in system.exceptions.values() if e["type"] == t]
        done = [e for e in es if e["resolved_day"] is not None]
        within = sum(1 for e in done if e["resolved_day"] <= e["due_day"])
        overdue = sum(1 for e in es if e["status"] == "open" and as_of + 0.99 > e["due_day"])
        sla[t] = {
            "raised": len(es),
            "resolved": len(done),
            "within_sla": round(within / len(done), 4) if done else None,
            "open": sum(1 for e in es if e["status"] == "open"),
            "open_overdue": overdue,
        }
    status = Counter(d["status"] for d in docs)
    return {
        "invoices": n,
        "touchless_rate": round(touchless / n, 4) if n else 0.0,
        "touchless": touchless,
        "exception_rate": round(sum(1 for d in docs if d["exceptions"]) / n, 4) if n else 0.0,
        "exception_rate_by_type": {t: round(by_type[t] / n, 4) for t in EXCEPTION_TYPES},
        "exceptions_by_type": {t: by_type[t] for t in EXCEPTION_TYPES},
        "cycle_hours": {
            "median": pct(cyc_all, 0.5),
            "p90": pct(cyc_all, 0.9),
            "touchless_median": pct(cyc_tl, 0.5),
            "with_exceptions_median": pct(cyc_ex, 0.5),
            "with_exceptions_p90": pct(cyc_ex, 0.9),
        },
        "cost_per_invoice": round(cost, 2),
        "cost_model": f"assumed: ${COST.system_per_invoice:.2f} system cost per invoice plus clerk time at "
        f"${COST.loaded_rate_per_hour:.0f} an hour per touch",
        "status": dict(status),
        "sla": sla,
        "discount": {
            k: schedule[k]
            for k in (
                "cost_of_cash",
                "discount_available",
                "discount_captured",
                "discount_missed",
                "discount_declined",
                "capture_rate",
            )
        },
        "dpo_days": schedule["dpo_days"],
        "paid_late": schedule["paid_late"],
        "open_exceptions": sum(1 for e in system.exceptions.values() if e["status"] == "open"),
    }


def series(system: APSystem) -> dict:
    """Weekly touchless rate by received week, and monthly discounts by pay month, for the dashboard charts."""
    weeks: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for d in system.docs.values():
        w = weeks[int(d["received_day"] // 7)]
        w[0] += 1
        w[1] += bool(d.get("touchless") and d["status"] in APPROVED)
    months: dict[str, dict] = defaultdict(lambda: {"captured": 0.0, "missed": 0.0, "declined": 0.0})
    for d in system.docs.values():
        p = d.get("payment")
        if not p or (d["approved_amount"] or 0) <= 0:
            continue
        m = months[p["pay_date"][:7]]
        pot = d["approved_amount"] * TERMS[system.suppliers[d["supplier_id"]].terms].discount_pct
        if p["take_discount"]:
            m["captured"] += p["discount"]
        elif p["reason"].startswith("missed"):
            m["missed"] += pot
        elif p["reason"].startswith("declined"):
            m["declined"] += pot
    return {
        "weekly": [
            {"week_start": to_date(k * 7).isoformat(), "invoices": v[0], "touchless_rate": round(v[1] / v[0], 4)}
            for k, v in sorted(weeks.items())
            if v[0] >= 20
        ],
        "monthly_discount": [{"month": k, **{x: round(y, 2) for x, y in v.items()}} for k, v in sorted(months.items())],
    }


def detection(system: APSystem) -> dict:
    """Precision and recall of each exception type against what the generator planted. Bank change follow-ups
    (later invoices on a real new account, before treasury approved it) are excluded: flagging them is correct."""
    out = {}
    docs = [d for d in system.docs.values() if d.get("truth")]
    for t in ["price", "quantity", "no_receipt", "duplicate", "bank_change", "closed_po", "non_po"]:
        tp = fp = fn = 0
        for d in docs:
            if t == "bank_change" and "bank_legit_followup" in d["truth"].get("planted", []):
                continue
            pred = any(system.exceptions[x]["type"] == t for x in d["exceptions"])
            real = t in d["truth"].get("expected", set())
            tp += pred and real
            fp += pred and not real
            fn += real and not pred
        out[t] = {
            "planted": tp + fn,
            "flagged": tp + fp,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        }
    return out


def controls(system: APSystem) -> dict:
    docs = [d for d in system.docs.values() if d.get("truth")]

    def planted(d, *tags):
        return bool(set(d["truth"].get("planted", [])) & set(tags))

    dups = [d for d in docs if planted(d, "duplicate", "duplicate_fuzzy")]
    fraud = [d for d in docs if planted(d, "bank_fraud")]
    closed = [d for d in docs if planted(d, "closed_po")]
    legit = [d for d in docs if planted(d, "bank_legit")]
    sod = 0
    for e in system.exceptions.values():
        if e["resolution"] == "accept_variance" and e["resolved_by"] == system.po_creator(system.docs[e["doc_id"]]):
            sod += 1
    return {
        "planted_duplicates": len(dups),
        "duplicates_approved": sum(1 for d in dups if d["status"] in APPROVED),
        "duplicate_dollars_blocked": round(sum(d["total"] or 0 for d in dups if d["status"] == "rejected"), 2),
        "planted_bank_fraud": len(fraud),
        "bank_fraud_approved": sum(1 for d in fraud if d["status"] in APPROVED),
        "bank_fraud_dollars_blocked": round(sum(d["total"] or 0 for d in fraud if d["status"] == "rejected"), 2),
        "legit_bank_changes": len({d["supplier_id"] for d in legit}),
        "legit_bank_changes_verified": sum(1 for s in system.suppliers.values() if s.bank_history),
        "closed_po_invoices": len(closed),
        "closed_po_approved": sum(1 for d in closed if d["status"] in APPROVED),
        "sod_violations": sod,
    }


def build_report(world, system, as_of, schedule, cfg: Config, extractor=None) -> dict:
    inv = world.invoices if cfg.capture_sample is None else world.invoices[: cfg.capture_sample]
    cap = evaluate(inv, world.suppliers, extractor)
    terms = Counter(s.terms for s in world.suppliers.values())
    return {
        "company": "Kestrel Ridge Fabrication (synthetic)",
        "seed": cfg.seed,
        "as_of": to_date(as_of).isoformat(),
        "counts": {
            "suppliers": len(world.suppliers),
            "purchase_orders": len(world.pos),
            "po_lines": sum(len(p.lines) for p in world.pos.values()),
            "receipts": len(world.receipts),
            "invoices": len(world.invoices),
            "credit_memos": sum(1 for i in world.invoices if i.is_credit),
            "pdf_invoices": sum(1 for i in world.invoices if i.channel == "pdf"),
        },
        "planted": dict(Counter(p for i in world.invoices for p in i.truth["planted"])),
        "kpis": kpis(system, as_of, schedule),
        "series": series(system),
        "detection": detection(system),
        "controls": controls(system),
        "capture": cap,
        "terms": {
            k: {"suppliers": v, "annualized_return": round(annualized_return(TERMS[k]), 4)} for k, v in terms.items()
        },
    }


def gate(report: dict, min_touchless: float) -> list[str]:
    """CI gate. Returns the reasons to fail (empty list means pass)."""
    c = report["controls"]
    fails = []
    if c["duplicates_approved"]:
        fails.append(f"{c['duplicates_approved']} planted duplicate invoices were approved for payment")
    if c["bank_fraud_approved"]:
        fails.append(f"{c['bank_fraud_approved']} invoices with a fraudulent bank change were approved for payment")
    if c["sod_violations"]:
        fails.append(f"{c['sod_violations']} variances were approved by the PO creator")
    tr = report["kpis"]["touchless_rate"]
    if tr < min_touchless:
        fails.append(f"touchless rate {tr:.1%} is below the {min_touchless:.1%} floor")
    return fails
