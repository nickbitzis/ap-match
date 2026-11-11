"""Seeded synthetic procure-to-pay data for Kestrel Ridge Fabrication, a made-up metal products manufacturer.

Suppliers, buyers, purchase orders with lines, goods receipts (partial, over, damaged and returned, late, missing),
and supplier invoices rendered as text in four layouts. Problems are planted at known rates and recorded in
`invoice.truth` so every detector can be scored."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .config import CATEGORIES, TERMS, TOLERANCES
from .layouts import LAYOUTS, render
from .model import Invoice, InvoiceLine, Person, POLine, PurchaseOrder, Receipt, Supplier

FIRST = [
    "Alder", "Basalt", "Copperline", "Dunmore", "Elkhorn", "Fairmont", "Granite", "Harbor", "Ironwood", "Juniper",
    "Keystone", "Larch", "Meridian", "Northgate", "Oakridge", "Pinecrest", "Quarry", "Redstone", "Silverton",
    "Tamarack", "Upland", "Valley", "Westbrook", "Yarrow", "Zephyr", "Bluewater", "Cinder", "Driftwood", "Emberly",
    "Foxglove", "Glenmore", "Hollis", "Ivory", "Jasper", "Kingsley", "Lowell", "Marlow", "Nettle", "Orchard",
]  # fmt: skip
SECOND = ["Ridge", "Point", "Creek", "Hill", "Falls", "Works", "Forge", "Bay", "Field", "Mill"]
SUFFIX = {
    "raw_materials": ["Metals", "Steel Service", "Alloys", "Aluminum"],
    "components": ["Fasteners", "Bearings", "Electronics", "Hydraulics"],
    "mro": ["Industrial Supply", "Tooling", "Safety Products", "Maintenance Supply"],
    "packaging": ["Packaging", "Corrugated", "Pallet Co", "Containers"],
    "services": ["Engineering Services", "Calibration", "Staffing", "Logistics"],
}
LEGAL = ["LLC", "Inc", "Co", "Ltd", "Corp"]
CAT_WEIGHTS = {"raw_materials": 0.28, "components": 0.26, "mro": 0.2, "packaging": 0.12, "services": 0.14}
PREFIX = {"raw_materials": "RM", "components": "CP", "mro": "MR", "packaging": "PK", "services": "SV"}
ITEM_WORDS = {
    "raw_materials": (
        ["Cold rolled steel sheet", "Hot rolled plate", "Aluminum bar 6061", "Stainless tube 304", "Copper busbar",
         "Steel angle", "Galvanized coil", "Brass rod"],
        ["16ga", "10ga", "1in", "2in", "3/8in", "48x96", "20ft", "1/4in"],
        (40, 900), "LB", (1.2, 9.0)),
    "components": (
        ["Hex bolt", "Ball bearing", "Proximity sensor", "Hydraulic fitting", "Lock nut", "Relay", "Gear motor",
         "Linear bushing"],
        ["M8x30", "6204-2RS", "24VDC", "1/2NPT", "M10", "SPDT", "0.5HP", "12mm"],
        (10, 800), "EA", (0.08, 240.0)),
    "mro": (
        ["Cutting oil", "Safety glasses", "Nitrile gloves", "Grinding disc", "Shop towels", "Drill bit set",
         "Hearing protection", "Welding wire"],
        ["5gal", "clear", "size L", "4.5in", "box of 200", "HSS 29pc", "NRR 32", "0.035in"],
        (2, 120), "EA", (4.0, 180.0)),
    "packaging": (
        ["Corrugated box", "Stretch film", "Wood pallet", "Foam insert", "Strapping", "Edge protector", "Poly bag",
         "Label roll"],
        ["24x18x12", "18in 80ga", "48x40", "custom", "1/2in", "36in", "12x18", "4x6"],
        (100, 5000), "EA", (0.05, 22.0)),
    "services": (
        ["Calibration service", "Field engineering", "Contract labor", "Machine repair", "Freight brokerage",
         "Quality audit", "CNC programming", "Equipment rental"],
        ["per hour", "per visit", "per day", "per job", "per load", "per audit", "per hour", "per week"],
        (1, 80), "HR", (45.0, 1400.0)),
}  # fmt: skip
FIRST_NAMES = ["Avery", "Blake", "Casey", "Devon", "Emery", "Finley", "Gray", "Harper", "Indy", "Jules", "Kai", "Lane"]
LAST_NAMES = ["Ashby", "Brook", "Calder", "Dane", "Ellison", "Frost", "Garner", "Hale", "Irving", "Jett", "Kerr"]


@dataclass
class Config:
    suppliers: int = 400
    pos: int = 25_000
    seed: int = 42
    days: int = 365


@dataclass
class World:
    suppliers: dict[str, Supplier]
    people: dict[str, Person]
    pos: dict[int, PurchaseOrder]
    receipts: list[Receipt]
    invoices: list[Invoice]
    catalog: dict[str, list[tuple[str, str, float, str]]]
    buyers_by_cat: dict[str, list[str]] = field(default_factory=dict)


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def make_people(rng: random.Random) -> tuple[dict[str, Person], dict[str, list[str]]]:
    people: dict[str, Person] = {}
    used: set[str] = set()

    def add(pid: str, role: str) -> None:
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in used:
                break
        used.add(name)
        people[pid] = Person(pid, name, role)

    buyers_by_cat: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for i in range(10):
        pid = f"B{i + 1:02d}"
        add(pid, "buyer")
        buyers_by_cat[CATEGORIES[i % len(CATEGORIES)]].append(pid)
    for prefix, role, n in [
        ("R", "receiving", 5),
        ("A", "ap_clerk", 6),
        ("T", "treasury", 3),
        ("S", "ap_supervisor", 2),
        ("M", "purchasing_manager", 2),
        ("C", "controller", 1),
        ("F", "cfo", 1),
    ]:
        for i in range(n):
            add(f"{prefix}{i + 1:02d}", role)
    return people, buyers_by_cat


def make_suppliers(rng: random.Random, n: int) -> dict[str, Supplier]:
    out: dict[str, Supplier] = {}
    names: set[str] = set()
    cats = list(CAT_WEIGHTS)
    terms_codes = list(TERMS)
    terms_w = [0.34, 0.14, 0.08, 0.26, 0.12, 0.06]
    for i in range(n):
        cat = rng.choices(cats, weights=[CAT_WEIGHTS[c] for c in cats])[0]
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(SECOND)} {rng.choice(SUFFIX[cat])} {rng.choice(LEGAL)}"
            if name not in names:
                break
        names.add(name)
        style = rng.choice(["INV-{:06d}", "{:d}", "A{:d}", "INV{:05d}", "{:d}-K"])
        out[f"S{i + 1:04d}"] = Supplier(
            id=f"S{i + 1:04d}",
            name=name,
            category=cat,
            terms=rng.choices(terms_codes, weights=terms_w)[0],
            routing=f"{rng.randint(10_000_000, 99_999_999)}{rng.randint(0, 9)}",
            account=str(rng.randint(10**9, 10**10 - 1)),
            layout=rng.choices(LAYOUTS, weights=[0.35, 0.25, 0.15, 0.25])[0],
            number_style=style,
            taxable=cat in ("mro", "packaging") and rng.random() < 0.5,
            tax_rate=0.0625,
        )
    return out


def make_catalog(rng: random.Random) -> dict[str, list[tuple[str, str, float, str]]]:
    cat: dict[str, list] = {}
    for c, (nouns, specs, _q, uom, (lo, hi)) in ITEM_WORDS.items():
        items = []
        k = 1001
        for noun in nouns:
            for spec in rng.sample(specs, 4):
                items.append((f"{PREFIX[c]}-{k}", f"{noun} {spec}", round(rng.uniform(lo, hi), 2), uom))
                k += 1
        cat[c] = items
    return cat


def reformat_number(num: str, rng: random.Random) -> str:
    """The same invoice number keyed differently: case, separators, zero padding, an INV prefix."""
    digits = "".join(ch for ch in num if ch.isdigit())
    letters = "".join(ch for ch in num if ch.isalpha())
    options = [
        num.lower(),
        f"{letters} {int(digits)}".strip() if digits else num.lower(),
        f"{letters}-{digits.zfill(8)}" if digits else num,
        f"#{num}",
        f"{letters}{int(digits)}" if letters and digits else f"INV{digits}",
    ]
    options = [o for o in options if o != num]
    return rng.choice(options)


def transpose(num: str, rng: random.Random) -> str:
    """A re-keyed number with two adjacent digits swapped (a fuzzy duplicate)."""
    idx = [i for i in range(len(num) - 1) if num[i].isdigit() and num[i + 1].isdigit() and num[i] != num[i + 1]]
    if not idx:
        return num + "A"
    i = rng.choice(idx)
    return num[:i] + num[i + 1] + num[i] + num[i + 2 :]


def generate(cfg: Config | None = None) -> World:
    cfg = cfg or Config()
    rng = random.Random(cfg.seed)
    people, buyers_by_cat = make_people(rng)
    suppliers = make_suppliers(rng, cfg.suppliers)
    catalog = make_catalog(rng)
    sup_list = list(suppliers.values())
    # Spend follows a long tail: a few suppliers get most of the orders.
    weights = [1.0 / (i + 1) ** 0.8 for i in range(len(sup_list))]
    price_factor = {s.id: rng.uniform(0.9, 1.15) for s in sup_list}
    inv_counter = {s.id: rng.randint(1000, 90000) for s in sup_list}
    managers = [p for p, x in people.items() if x.role == "purchasing_manager"]
    receivers = [p for p, x in people.items() if x.role == "receiving"]

    pos: dict[int, PurchaseOrder] = {}
    receipts: list[Receipt] = []
    invoices: list[Invoice] = []
    inv_seq = 0
    legit_changes: dict[str, list[tuple[float, str, str]]] = {}

    def next_number(s: Supplier) -> str:
        inv_counter[s.id] += rng.randint(1, 40)
        return s.number_style.format(inv_counter[s.id])

    def new_invoice(s: Supplier, po_id, lines, inv_day, truth, **kw) -> Invoice:
        nonlocal inv_seq
        inv_seq += 1
        routing, account = s.routing, s.account
        total = _money(sum(x.amount for x in lines))
        inv = Invoice(
            id=f"I{inv_seq:06d}",
            supplier_id=s.id,
            number=kw.pop("number", None) or next_number(s),
            invoice_day=float(int(inv_day)),
            received_day=kw.pop("received_day", None) or int(inv_day) + rng.randint(0, 4) + rng.uniform(0.3, 0.7),
            po_id=po_id,
            lines=lines,
            total=total,
            routing=kw.pop("routing", routing),
            account=kw.pop("account", account),
            layout=s.layout,
            truth=truth,
            **kw,
        )
        invoices.append(inv)
        return inv

    def add_charges(s: Supplier, lines: list[InvoiceLine], truth: dict) -> None:
        r = rng.random()
        if r < 0.06:
            lines.append(
                InvoiceLine(len(lines) + 1, None, "Freight", None, None, _money(rng.uniform(15, 74)), "freight")
            )
            truth["planted"].append("freight_small")
        elif r < 0.08:
            lines.append(
                InvoiceLine(len(lines) + 1, None, "Freight", None, None, _money(rng.uniform(140, 900)), "freight")
            )
            truth["planted"].append("freight_big")
            truth["expected"].add("non_po")
        if s.taxable:
            taxable = sum(x.amount for x in lines if x.kind == "item")
            tax = _money(taxable * s.tax_rate)
            if rng.random() < 0.05:
                tax = _money(tax * rng.uniform(1.3, 1.8) + 5)
                truth["planted"].append("tax_wrong")
                truth["expected"].add("non_po")
            lines.append(InvoiceLine(len(lines) + 1, None, f"Sales tax {s.tax_rate:.2%}", None, None, tax, "tax"))

    for po_id in range(1, cfg.pos + 1):
        s = rng.choices(sup_list, weights=weights)[0]
        day = rng.uniform(0, cfg.days - 15)
        items = rng.sample(catalog[s.category], rng.choice([1, 1, 2, 2, 3, 4]))
        qlo, qhi = ITEM_WORDS[s.category][2]
        plines = []
        for i, (code, desc, base, uom) in enumerate(items, start=1):
            qty = float(rng.randint(qlo, qhi))
            price = base * price_factor[s.id]
            plines.append(POLine(i, code, desc, qty, round(price, 4 if price < 1 else 2), uom, s.category))
        po_total = sum(x.amount for x in plines)
        creator = rng.choice(managers) if po_total > 60_000 else rng.choice(buyers_by_cat[s.category])
        po = PurchaseOrder(po_id, s.id, creator, day, s.category, plines)
        pos[po_id] = po
        three_way = TOLERANCES[s.category].three_way
        tol = TOLERANCES[s.category]

        # ---- receipts
        r = rng.random()
        scenario = "full"
        if three_way:
            if r < 0.21 and min(ln.qty for ln in plines) < 4:
                scenario = "full"  # too few units to split into two shipments
            elif r < 0.18:
                scenario = "split2"  # two shipments, two invoices
            elif r < 0.21:
                scenario = "split1"  # two shipments, one invoice (one invoice line to many receipts)
            elif r < 0.23 and tol.qty_over_pct > 0:
                scenario = "over_ok"
            elif r < 0.245:
                scenario = "damaged_net"
            elif r < 0.255:
                scenario = "damaged_billed"
            elif r < 0.27:
                scenario = "qty_over_billed"
            elif r < 0.282:
                scenario = "no_receipt"
            elif r < 0.30:
                scenario = "late_receipt"
        elif r < 0.03:
            scenario = "hours_over"
        recv_day = day + rng.randint(3, 20) + rng.uniform(0.3, 0.6)
        rcpts: dict[int, list[Receipt]] = {ln.line_no: [] for ln in plines}

        def rec(line_no: int, d: float, q: float, ret: float = 0.0, note: str = "") -> None:
            rid = f"R{len(receipts) + 1:06d}"
            x = Receipt(rid, po_id, line_no, d, q, ret, rng.choice(receivers), note)
            receipts.append(x)
            rcpts[line_no].append(x)

        gap = rng.randint(5, 15)  # days between the two shipments of a split delivery
        if three_way and scenario != "no_receipt":
            for ln in plines:
                if scenario in ("split2", "split1"):
                    first = float(int(ln.qty * rng.choice([0.4, 0.5, 0.6])))
                    rec(ln.line_no, recv_day, first)
                    rec(ln.line_no, recv_day + gap, ln.qty - first)
                elif scenario == "over_ok":
                    rec(ln.line_no, recv_day, float(int(ln.qty * (1 + tol.qty_over_pct * rng.uniform(0.3, 0.9)))))
                elif scenario in ("damaged_net", "damaged_billed"):
                    bad = max(1.0, float(int(ln.qty * rng.uniform(0.05, 0.2))))
                    rec(ln.line_no, recv_day, ln.qty, bad, "damaged, returned to supplier")
                elif scenario == "late_receipt":
                    rec(ln.line_no, recv_day + rng.randint(8, 14), ln.qty)
                else:
                    rec(ln.line_no, recv_day, ln.qty)

        # ---- invoices
        price_mod = rng.random()
        bank_r = rng.random()
        closed = rng.random() < 0.006

        def bill(lines_qty: list[tuple[POLine, float]], inv_day: float, extra_planted=()) -> Invoice:
            truth = {"planted": [scenario] if scenario != "full" else [], "expected": set(), "should_pay": True}
            truth["planted"] += list(extra_planted)
            lines: list[InvoiceLine] = []
            for ln, q in lines_qty:
                price = ln.unit_price
                if price_mod < 0.08 and tol.price_pct >= 0.01:
                    # A small increase inside both the percent and the dollar tolerance. It should pass untouched.
                    diff = min(
                        ln.unit_price * rng.uniform(0.0005, tol.price_pct * 0.8), tol.price_abs * 0.8 / max(q, 1.0)
                    )
                    price = round(ln.unit_price + diff, 4)
                    truth["planted"].append("price_within")
                elif price_mod < 0.12:
                    price = round(price * (1 + rng.uniform(0.04, 0.15)), 4)
                    truth["planted"].append("price_over")
                    truth["expected"].add("price")
                if rng.random() < 0.04 and q >= 4:
                    # The supplier splits one PO line into two invoice lines (two lots). Many-to-one.
                    a = float(int(q / 2))
                    for part in (a, q - a):
                        lines.append(
                            InvoiceLine(len(lines) + 1, ln.item, ln.description, part, price, _money(part * price))
                        )
                    truth["planted"].append("split_lines")
                else:
                    lines.append(InvoiceLine(len(lines) + 1, ln.item, ln.description, q, price, _money(q * price)))
            truth["planted"] = list(dict.fromkeys(truth["planted"]))
            add_charges(s, lines, truth)
            kw = {}
            if bank_r < 0.004:
                kw["routing"] = f"{rng.randint(10_000_000, 99_999_999)}{rng.randint(0, 9)}"
                kw["account"] = str(rng.randint(10**9, 10**10 - 1))
                truth["planted"].append("bank_fraud")
                truth["expected"].add("bank_change")
                truth["should_pay"] = False
            if closed:
                truth["planted"].append("closed_po")
                truth["expected"] = {"closed_po"}
                truth["should_pay"] = False
            inv = new_invoice(s, po_id, lines, inv_day, truth, **kw)
            if 0.004 <= bank_r < 0.0046:
                # A real bank change: from this invoice on, the supplier bills with the new account.
                legit_changes.setdefault(s.id, []).append(
                    (
                        inv.received_day,
                        f"{rng.randint(10_000_000, 99_999_999)}{rng.randint(0, 9)}",
                        str(rng.randint(10**9, 10**10 - 1)),
                    )
                )
                truth["planted"].append("bank_legit")
                truth["expected"].add("bank_change")
            return inv

        inv_day = recv_day + 1 + rng.randint(0, 5)  # suppliers invoice after shipping, so after receipt
        if scenario == "split2":
            first = [(ln, rcpts[ln.line_no][0].qty_received) for ln in plines]
            second = [(ln, rcpts[ln.line_no][1].qty_received) for ln in plines]
            bill(first, inv_day)
            bill(second, rcpts[plines[0].line_no][1].day + 1 + rng.randint(0, 4))
        elif scenario == "split1":
            bill([(ln, ln.qty) for ln in plines], rcpts[plines[0].line_no][1].day + 1 + rng.randint(0, 3))
        elif scenario == "over_ok":
            bill([(ln, sum(x.qty_received for x in rcpts[ln.line_no])) for ln in plines], inv_day)
        elif scenario == "damaged_net":
            bill([(ln, sum(x.net_qty for x in rcpts[ln.line_no])) for ln in plines], inv_day)
        elif scenario == "damaged_billed":
            inv = bill([(ln, ln.qty) for ln in plines], inv_day)
            inv.truth["expected"].add("quantity")
        elif scenario == "qty_over_billed":
            inv = bill(
                [(ln, ln.qty + max(1.0, float(int(ln.qty * rng.uniform(0.08, 0.25))))) for ln in plines], inv_day
            )
            inv.truth["expected"].add("quantity")
        elif scenario == "hours_over":
            inv = bill([(ln, ln.qty + max(1.0, float(int(ln.qty * 0.2)))) for ln in plines], day + rng.randint(5, 25))
            inv.truth["expected"].add("quantity")
        elif scenario in ("no_receipt", "late_receipt"):
            inv = bill([(ln, ln.qty) for ln in plines], recv_day + rng.randint(0, 3))
            inv.truth["expected"].add("no_receipt")
            if scenario == "no_receipt":
                inv.truth["should_pay"] = False if not closed else inv.truth["should_pay"]
        else:
            day_for_services = day + rng.randint(5, 25) if not three_way else inv_day
            bill([(ln, ln.qty) for ln in plines], day_for_services)
        if closed:
            # Closed before the invoice arrived: the order was cancelled or force-closed by the buyer.
            for inv in invoices[-2:]:
                if inv.po_id == po_id:
                    inv.truth["expected"] = {"closed_po"}
            po.status = "closed"
            po.closed_day = min(i.received_day for i in invoices if i.po_id == po_id) - 1.0

    _plant_non_po(rng, cfg, suppliers, invoices, new_invoice, add_charges)
    _plant_duplicates_and_credits(rng, suppliers, invoices, new_invoice)
    invoices.sort(key=lambda x: x.received_day)
    _apply_bank_changes(invoices, legit_changes)
    _render_all(rng, suppliers, invoices)
    return World(suppliers, people, pos, receipts, invoices, catalog, buyers_by_cat)


def _plant_non_po(rng, cfg, suppliers, invoices, new_invoice, add_charges) -> None:
    svc = [s for s in suppliers.values() if s.category in ("services", "mro")]
    for _ in range(int(cfg.pos * 0.015)):
        s = rng.choice(svc)
        amt = _money(rng.uniform(80, 4000))
        truth = {"planted": ["non_po"], "expected": {"non_po"}, "should_pay": True}
        lines = [
            InvoiceLine(
                1, None, rng.choice(["Emergency repair call-out", "Monthly service fee", "Rush job"]), 1.0, amt, amt
            )
        ]
        new_invoice(s, None, lines, rng.uniform(10, cfg.days - 5), truth)


def _plant_duplicates_and_credits(rng, suppliers, invoices, new_invoice) -> None:
    originals = [i for i in invoices if i.po_id is not None and not i.truth["expected"]]
    for orig in rng.sample(originals, int(len(invoices) * 0.015)):
        s = suppliers[orig.supplier_id]
        fuzzy = rng.random() < 0.3
        num = transpose(orig.number, rng) if fuzzy else reformat_number(orig.number, rng)
        lines = [
            InvoiceLine(x.line_no, x.item, x.description, x.qty, x.unit_price, x.amount, x.kind) for x in orig.lines
        ]
        truth = {
            "planted": ["duplicate_fuzzy" if fuzzy else "duplicate"],
            "expected": {"duplicate"},
            "should_pay": False,
            "duplicate_of": orig.id,
        }
        new_invoice(
            s,
            orig.po_id,
            lines,
            orig.invoice_day + rng.randint(0, 3),
            truth,
            number=num,
            received_day=orig.received_day + rng.uniform(3, 25),
            routing=orig.routing,
            account=orig.account,
        )
    for orig in rng.sample(originals, int(len(invoices) * 0.01)):
        s = suppliers[orig.supplier_id]
        amt = -_money(orig.subtotal * rng.choice([0.02, 0.05, 0.1]))
        truth = {"planted": ["credit_memo"], "expected": set(), "should_pay": True, "credit_for": orig.id}
        new_invoice(
            s,
            orig.po_id,
            [InvoiceLine(1, None, f"Credit for invoice {orig.number}", None, None, amt)],
            orig.invoice_day + rng.randint(10, 30),
            truth,
            number=f"CM{rng.randint(1000, 99999)}",
            is_credit=True,
            original_number=orig.number,
            routing=orig.routing,
            account=orig.account,
        )


def _apply_bank_changes(invoices: list[Invoice], changes: dict[str, list[tuple[float, str, str]]]) -> None:
    """Invoices carry whichever account the supplier used on the day they were sent. Later invoices on a new,
    legitimate account are follow-ups: they are flagged until treasury verifies the change, which is correct."""
    for v in changes.values():
        v.sort()
    for inv in invoices:
        if "bank_fraud" in inv.truth["planted"]:
            continue
        current = None
        for day, routing, account in changes.get(inv.supplier_id, []):
            if day <= inv.received_day:
                current = (routing, account)
        if current:
            inv.routing, inv.account = current
            if "bank_legit" not in inv.truth["planted"]:
                inv.truth["planted"].append("bank_legit_followup")


def _render_all(rng, suppliers, invoices) -> None:
    for inv in invoices:
        noise = {}
        if inv.layout == "letter" and rng.random() < 0.25:
            # A scanned letter. Most OCR slips are a letter O in an amount, which the parser repairs.
            n_items = sum(1 for x in inv.lines if x.kind == "item" and x.qty is not None)
            if n_items:
                if rng.random() < 0.2:
                    noise["drop_decimal"] = rng.randrange(n_items)
                    inv.truth["planted"].append("ocr_decimal")
                else:
                    noise["ocr_o"] = rng.randrange(n_items)
                    inv.truth["planted"].append("ocr_o")
            inv.channel = "scan"
        elif rng.random() < 0.01:
            inv.channel = "pdf"
        inv.text = render(inv, suppliers[inv.supplier_id], noise)
