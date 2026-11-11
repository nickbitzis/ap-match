import random

import pytest

from apmatch.controls import (
    DuplicateIndex,
    SoDViolation,
    check_sod,
    eligible_roles,
    near,
    normalize_number,
    required_level,
    route_approval,
)
from apmatch.generator import make_people, reformat_number, transpose
from apmatch.model import Person


@pytest.mark.parametrize(
    "a,b",
    [
        ("INV-004512", "inv 4512"),
        ("INV-004512", "#INV4512"),
        ("4512-K", "K-00004512"),
        ("A4512", "a 4512"),
        ("INV04512", "4512"),
        ("45O12", "45012"),
    ],
)
def test_normalize_number_ignores_formatting(a, b):
    assert normalize_number(a) == normalize_number(b)


def test_normalize_keeps_meaningful_letters():
    assert normalize_number("A4512") != normalize_number("B4512")


def test_generator_reformats_normalize_to_the_same_number():
    rng = random.Random(0)
    for num in ["INV-004512", "4512", "A4512", "INV04512", "4512-K"]:
        for _ in range(10):
            assert normalize_number(reformat_number(num, rng)) == normalize_number(num)


def test_near_numbers():
    assert near("|4512", "|4521") and near("|4512", "|4513")
    assert not near("|4512", "|4512") and not near("|4512", "|5412x") and not near("|4512", "|4623")
    assert near(normalize_number("4512"), normalize_number(transpose("4512", random.Random(1))))


def d(i, number, total, po=1, day=10.0, items=(("CP-1", 10.0),), credit=False, sup="S1"):
    return {
        "id": i,
        "supplier_id": sup,
        "number": number,
        "total": total,
        "invoice_day": day,
        "po_id": po,
        "doc_type": "credit_memo" if credit else "invoice",
        "lines": [{"item": it, "qty": q, "kind": "item"} for it, q in items],
    }


def test_same_number_after_formatting_is_a_duplicate():
    idx = DuplicateIndex()
    idx.add(d("A", "INV-004512", 100.0))
    hit = idx.check(d("B", "inv 4512", 100.0, day=40))
    assert hit and hit.rule == "same_number" and hit.of == "A"


def test_other_supplier_is_never_a_duplicate():
    idx = DuplicateIndex()
    idx.add(d("A", "4512", 100.0))
    assert idx.check(d("B", "4512", 100.0, sup="S2")) is None


def test_fuzzy_number_same_amount_within_window():
    idx = DuplicateIndex()
    idx.add(d("A", "4512", 100.0, po=1))
    hit = idx.check(d("B", "4521", 100.0, po=2, items=(("CP-9", 1.0),), day=20))
    assert hit and hit.rule == "fuzzy_number"
    assert idx.check(d("C", "4521", 100.0, po=2, day=200)) is None  # outside the 60 day window
    assert idx.check(d("D", "4521", 100.5, po=2)) is None  # different amount


def test_same_content_under_a_new_number():
    idx = DuplicateIndex()
    idx.add(d("A", "4512", 100.0))
    hit = idx.check(d("B", "9981", 100.0, day=15))
    assert hit and hit.rule == "same_content"


def test_hard_negative_second_delivery_is_not_a_duplicate():
    idx = DuplicateIndex()
    idx.add(d("A", "4512", 100.0))
    assert idx.check(d("B", "4513", 100.0, day=25), covered=lambda prev: True) is None


def test_credit_memos_and_invoices_do_not_collide():
    idx = DuplicateIndex()
    idx.add(d("A", "4512", 100.0))
    assert idx.check(d("B", "4512", -100.0, credit=True)) is None
    assert idx.find("S1", "INV 4512")["id"] == "A"


def test_removed_documents_are_not_matched():
    idx = DuplicateIndex()
    first = d("A", "4512", 100.0)
    idx.add(first)
    idx.remove(first)
    assert idx.check(d("B", "4512", 100.0)) is None


def test_sod_blocks_po_creator_and_self_approval():
    with pytest.raises(SoDViolation):
        check_sod("B01", "accept_variance", po_creator="B01")
    with pytest.raises(SoDViolation):
        check_sod("T01", "approve_bank_change", None, requested_by="T01")
    check_sod("S01", "accept_variance", po_creator="B01")  # no error


@pytest.mark.parametrize(
    "variance,total,level",
    [(100, 5_000, 0), (600, 8_000, 1), (100, 30_000, 1), (6_000, 300_000, 2), (60_000, 900_000, 3), (900, 5_000, 2)],
)
def test_required_level_by_amount_and_variance(variance, total, level):
    assert required_level(variance, total) == level


def test_route_skips_po_creator_and_escalates():
    people, _ = make_people(random.Random(1))
    r = route_approval(1_000, 40_000, people, exclude={"M01"}, key="E1")
    assert r["role"] == "purchasing_manager" and r["approver"] == "M02" and not r["escalated"]
    r = route_approval(1_000, 40_000, people, exclude={"M01", "M02"}, key="E1")
    assert r["role"] == "controller" and r["escalated"]
    assert "nobody eligible" in r["path"][0]


def test_route_with_no_eligible_approver_raises():
    people = {"F01": Person("F01", "x", "cfo")}
    with pytest.raises(SoDViolation):
        route_approval(10, 100, people, exclude={"F01"})


def test_eligible_roles_climb_the_ladder():
    assert eligible_roles(100, 5_000) == {"ap_supervisor", "purchasing_manager", "controller", "cfo"}
    assert eligible_roles(10_000, 100_000) == {"controller", "cfo"}
