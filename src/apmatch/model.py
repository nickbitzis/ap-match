"""Domain records. Times are float days since 2025-01-01 (day 0.0 is midnight on January 1, 2025)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

EPOCH = datetime(2025, 1, 1)


def to_iso(day: float | None) -> str | None:
    if day is None:
        return None
    return (EPOCH + timedelta(days=day)).isoformat(timespec="minutes")


def to_date(day: float) -> date:
    return (EPOCH + timedelta(days=int(day))).date()


def from_date(d: date) -> float:
    return float((d - EPOCH.date()).days)


@dataclass
class Supplier:
    id: str
    name: str
    category: str
    terms: str
    routing: str
    account: str
    layout: str
    number_style: str
    taxable: bool
    tax_rate: float
    bank_history: list = field(default_factory=list)  # (day, routing, account, verified_by, approved_by)


@dataclass
class Person:
    id: str
    name: str
    role: str  # buyer, receiving, ap_clerk, treasury, ap_supervisor, purchasing_manager, controller, cfo


@dataclass
class POLine:
    line_no: int
    item: str
    description: str
    qty: float
    unit_price: float
    uom: str
    category: str

    @property
    def amount(self) -> float:
        return round(self.qty * self.unit_price, 2)


@dataclass
class PurchaseOrder:
    id: int
    supplier_id: str
    created_by: str
    created_day: float
    category: str
    lines: list[POLine]
    status: str = "open"  # open | closed
    closed_day: float | None = None

    @property
    def number(self) -> str:
        return f"PO-{self.id:07d}"

    @property
    def total(self) -> float:
        return round(sum(x.amount for x in self.lines), 2)

    def line(self, line_no: int) -> POLine | None:
        return next((x for x in self.lines if x.line_no == line_no), None)


@dataclass
class Receipt:
    id: str
    po_id: int
    line_no: int
    day: float
    qty_received: float
    qty_returned: float = 0.0
    posted_by: str = ""
    note: str = ""

    @property
    def net_qty(self) -> float:
        return self.qty_received - self.qty_returned


@dataclass
class InvoiceLine:
    line_no: int
    item: str | None
    description: str
    qty: float | None
    unit_price: float | None
    amount: float
    kind: str = "item"  # item | freight | tax


@dataclass
class Invoice:
    """The document a supplier sent. `truth` holds what the generator planted; the pipeline never reads it
    except to score itself and to answer the outside world's questions (did the supplier confirm a bank change?)."""

    id: str
    supplier_id: str
    number: str
    invoice_day: float
    received_day: float
    po_id: int | None
    lines: list[InvoiceLine]
    total: float
    routing: str
    account: str
    layout: str
    is_credit: bool = False
    original_number: str | None = None
    text: str = ""
    channel: str = "email"
    truth: dict = field(default_factory=dict)

    @property
    def subtotal(self) -> float:
        return round(sum(x.amount for x in self.lines if x.kind == "item"), 2)
