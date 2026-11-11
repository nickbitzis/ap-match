"""Policy as data: tolerances by category, payment terms, owners, SLAs, approval levels, and the cost model.

Everything a controller would want to change without a code release lives here."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tolerance:
    """A line passes the price check only if BOTH the percent and the absolute dollar variance are within limits.
    `qty_over_pct` is how much over-delivery a category accepts (for bulk goods sold by weight or length)."""

    price_pct: float
    price_abs: float
    qty_over_pct: float
    three_way: bool = True


TOLERANCES: dict[str, Tolerance] = {
    "raw_materials": Tolerance(price_pct=0.02, price_abs=150.0, qty_over_pct=0.05),
    "components": Tolerance(price_pct=0.01, price_abs=50.0, qty_over_pct=0.0),
    "mro": Tolerance(price_pct=0.05, price_abs=25.0, qty_over_pct=0.0),
    "packaging": Tolerance(price_pct=0.03, price_abs=75.0, qty_over_pct=0.10),
    # Services have no goods receipt, so they get a two-way match (PO and invoice only).
    "services": Tolerance(price_pct=0.0005, price_abs=1.0, qty_over_pct=0.0, three_way=False),
}

CATEGORIES = list(TOLERANCES)

# Freight that is not on the PO is accepted without a human below this amount. Tax lines are accepted when they
# equal the supplier's tax rate times the taxable subtotal, to the cent (with a 2 cent rounding allowance).
FREIGHT_AUTO_ACCEPT = 75.0
TAX_ROUNDING = 0.02


@dataclass(frozen=True)
class Terms:
    code: str
    discount_pct: float  # 0.02 for 2/10 net 30
    discount_days: int
    net_days: int


TERMS: dict[str, Terms] = {
    "2/10 net 30": Terms("2/10 net 30", 0.02, 10, 30),
    "1/10 net 30": Terms("1/10 net 30", 0.01, 10, 30),
    "0.5/10 net 45": Terms("0.5/10 net 45", 0.005, 10, 45),
    "net 30": Terms("net 30", 0.0, 0, 30),
    "net 45": Terms("net 45", 0.0, 0, 45),
    "net 60": Terms("net 60", 0.0, 0, 60),
}

# Exception types, who owns them, and how long they have before the SLA is breached (hours).
EXCEPTION_TYPES = ["capture", "price", "quantity", "no_receipt", "duplicate", "bank_change", "non_po", "closed_po"]
OWNERS = {
    "capture": "ap_clerk",
    "price": "buyer",
    "quantity": "receiving",
    "no_receipt": "receiving",
    "duplicate": "ap_clerk",
    "bank_change": "treasury",
    "non_po": "ap_clerk",
    "closed_po": "buyer",
}
SLA_HOURS = {
    "capture": 8,
    "price": 48,
    "quantity": 48,
    "no_receipt": 72,
    "duplicate": 24,
    "bank_change": 24,
    "non_po": 48,
    "closed_po": 48,
}
# Which resolutions make sense for each type. The API refuses anything else.
ALLOWED_ACTIONS = {
    "capture": {"correct_fields", "hold"},
    "price": {"accept_variance", "request_credit_memo", "short_pay", "hold"},
    "quantity": {"accept_variance", "request_credit_memo", "short_pay", "hold"},
    "no_receipt": {"receipt_posted", "short_pay", "hold", "reject"},
    "duplicate": {"reject", "not_duplicate", "hold"},
    "bank_change": {"callback_confirmed", "callback_failed", "approve_bank_change", "hold"},
    "non_po": {"accept_variance", "request_credit_memo", "short_pay", "hold", "reject"},
    "closed_po": {"reject", "accept_variance", "hold"},
}


@dataclass(frozen=True)
class ApprovalLevel:
    role: str
    max_variance: float
    max_invoice: float


# Variance approvals climb this ladder. A variance above 10% of the invoice skips one level.
APPROVAL_LEVELS = [
    ApprovalLevel("ap_supervisor", 500.0, 25_000.0),
    ApprovalLevel("purchasing_manager", 5_000.0, 250_000.0),
    ApprovalLevel("controller", 50_000.0, 1_000_000.0),
    ApprovalLevel("cfo", float("inf"), float("inf")),
]
LARGE_VARIANCE_PCT = 0.10


@dataclass(frozen=True)
class CostModel:
    """Cost per invoice. These are assumptions for the demo, not measurements: a system cost per invoice plus
    loaded clerk time for each human touch."""

    system_per_invoice: float = 0.85
    loaded_rate_per_hour: float = 48.0
    touch_minutes: dict = field(
        default_factory=lambda: {
            "capture": 6,
            "price": 22,
            "quantity": 20,
            "no_receipt": 25,
            "duplicate": 8,
            "bank_change": 35,
            "non_po": 15,
            "closed_po": 12,
            "approval": 5,
        }
    )


COST = CostModel()

# Annual cost of cash. Take an early payment discount only when its annualized return beats this.
COST_OF_CASH = 0.08
PAYMENT_RUN_WEEKDAYS = (1, 3)  # Tuesday and Thursday
CAPTURE_CONFIDENCE_FLOOR = 0.80
DUPLICATE_WINDOW_DAYS = 60
