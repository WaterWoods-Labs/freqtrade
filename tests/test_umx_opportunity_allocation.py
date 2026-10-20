from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from freqtrade.umx_opportunity_allocation import allocate


D = Decimal


def candidate(pair, **kwargs):
    return {"pair": pair, "rate": D("1"), "step": D("0.01"), **kwargs}


def test_single_candidate_receives_budget_without_reserving_absent_opportunities():
    plans = allocate([candidate("A")], D("950"), 5)
    assert plans[0]["target"] == plans[0]["notional"] == D("950")


def test_equal_allocation_preserves_order_metadata_and_inputs():
    candidates = [
        candidate(pair, signal_id=f"{pair}|signal", extra={"original": 1}) for pair in "BA"
    ]
    original = deepcopy(candidates)
    plans = allocate(candidates, D("950"), 5)
    assert [plan["pair"] for plan in plans] == ["B", "A"]
    assert [plan["target"] for plan in plans] == [D("475"), D("475")]
    assert [plan["signal_id"] for plan in plans] == ["B|signal", "A|signal"]
    assert all(plan["extra"] == {"original": 1} for plan in plans)
    assert candidates == original
    assert all(plan is not source for plan, source in zip(plans, candidates, strict=True))
    assert allocate(candidates, D("950"), 5) == plans


def test_capacity_selects_first_five_even_if_later_candidate_is_cheaper():
    plans = allocate([candidate(pair) for pair in "ABCDEF"], D("950"), 5)
    assert [plan["pair"] for plan in plans] == list("ABCDE")
    assert [plan["target"] for plan in plans] == [D("190")] * 5


def test_caps_waterfill_in_multiple_rounds():
    plans = allocate(
        [candidate("A", max_amount=D("100")), candidate("B", max_cost=D("350")), candidate("C")],
        D("950"),
        5,
    )
    assert [plan["target"] for plan in plans] == [D("100"), D("350"), D("500")]


def test_maximum_caps_are_floored_and_both_limits_apply():
    plans = allocate(
        [
            candidate("A", rate=D("3"), step=D("2"), max_amount=D("9"), max_cost=D("20")),
            candidate("B"),
        ],
        D("100"),
        5,
    )
    assert plans[0]["amount"] == D("6")
    assert [plan["target"] for plan in plans] == [D("18"), D("82")]


def test_all_caps_leave_cash_and_do_not_add_an_unselected_candidate():
    plans = allocate([candidate(pair, max_cost=D("100")) for pair in "ABC"], D("950"), 2)
    assert [plan["pair"] for plan in plans] == ["A", "B"]
    assert sum(plan["notional"] for plan in plans) == D("200")


def test_equal_cap_is_applied_without_infinite_waterfill():
    plans = allocate([candidate("A", max_cost=D("475")), candidate("B")], D("950"), 5)
    assert [plan["target"] for plan in plans] == [D("475"), D("475")]


def test_minimum_failures_remove_only_last_failing_candidate_then_replan():
    plans = allocate([candidate(pair, min_cost=D("600")) for pair in "AB"], D("950"), 5)
    assert [plan["pair"] for plan in plans] == ["A"]
    assert plans[0]["target"] == D("950")


def test_reverse_priority_pruning_backfills_without_reviving_rejected_candidates():
    plans = allocate([candidate(pair, min_cost=D("300")) for pair in "ABCDEF"], D("950"), 5)
    # Initial ABCDE drops E; ABCDF drops F; ABCD drops D; ABC survive.
    assert [plan["pair"] for plan in plans] == list("ABC")
    assert [plan["notional"] for plan in plans] == [D("316.66")] * 3


def test_last_failure_is_removed_instead_of_last_selected_candidate():
    plans = allocate(
        [candidate("A", min_cost=D("600")), candidate("B"), candidate("C", min_cost=D("600"))],
        D("950"),
        5,
    )
    assert [plan["pair"] for plan in plans] == ["B"]
    assert plans[0]["target"] == D("950")


@pytest.mark.parametrize(
    "limits",
    [
        {"min_cost": D("951")},
        {"min_cost": D("600"), "max_cost": D("500")},
        {"min_amount": D("1"), "max_amount": D("0")},
        {"step": D("1000")},
    ],
)
def test_individually_impossible_candidate_does_not_consume_a_slot(limits):
    plans = allocate([candidate("A", **limits), candidate("B")], D("950"), 1)
    assert [plan["pair"] for plan in plans] == ["B"]
    assert plans[0]["notional"] == D("950")


def test_rounding_dust_is_never_redistributed():
    plans = allocate([candidate("A", step=D("100")), candidate("B")], D("950"), 5)
    assert [plan["target"] for plan in plans] == [D("475"), D("475")]
    assert [plan["notional"] for plan in plans] == [D("400"), D("475")]


def test_base_step_already_includes_contract_size_and_lot():
    plans = allocate(
        [candidate("A", rate=D("3000"), step=D("0.001"), min_amount=D("0.002"))],
        D("20"),
        5,
    )
    assert plans[0]["amount"] == D("0.006")
    assert plans[0]["notional"] == D("18")


def test_rounded_minimum_amount_and_cost_are_checked_at_actual_notional():
    plans = allocate(
        [candidate("A", rate=D("3"), step=D("2"), min_cost=D("19")), candidate("B")],
        D("40"),
        5,
    )
    assert [plan["pair"] for plan in plans] == ["B"]
    assert plans[0]["notional"] == D("40")


def test_exact_minimum_is_accepted():
    plans = allocate(
        [candidate("A", rate=D("3"), step=D("2"), min_amount=D("6"), min_cost=D("18"))],
        D("18"),
        5,
    )
    assert plans[0]["amount"] == D("6")


def test_decimal_floor_remains_safe_below_a_step_and_under_low_caller_precision():
    budget = D("0.9999999999999999999999999999999999999999")
    with localcontext() as context:
        context.prec = 4
        plans = allocate([candidate("A", step=D("0.1"))], budget, 5)
        assert context.prec == 4
    assert plans[0]["amount"] == D("0.9")


def test_zero_budget_slots_or_candidates_return_empty():
    assert allocate([], D("950"), 5) == []
    assert allocate([candidate("A")], D("0"), 5) == []
    assert allocate([candidate("A")], D("950"), 0) == []


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-1"), 950.0, "950", None])
def test_invalid_budget_is_rejected(value):
    with pytest.raises(ValueError, match="available_budget"):
        allocate([], value, 5)


@pytest.mark.parametrize("slots", [-1, 1.5, True, None])
def test_invalid_slots_are_rejected(slots):
    with pytest.raises(ValueError, match="free_slots"):
        allocate([], D("950"), slots)


@pytest.mark.parametrize(
    "field", ["rate", "step", "min_amount", "max_amount", "min_cost", "max_cost"]
)
@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-1"), 1.0, "1"])
def test_invalid_candidate_numbers_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        allocate([candidate("A", **{field: value})], D("950"), 5)


@pytest.mark.parametrize("field", ["rate", "step"])
@pytest.mark.parametrize("value", [D("0"), None])
def test_required_positive_candidate_numbers_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        allocate([candidate("A", **{field: value})], D("950"), 5)


@pytest.mark.parametrize("candidates", [None, (), [None], [{}], [candidate("")], [candidate(" ")]])
def test_invalid_candidate_structure_is_rejected(candidates):
    with pytest.raises(ValueError):
        allocate(candidates, D("950"), 5)


def test_duplicate_pair_is_rejected_even_without_available_budget():
    with pytest.raises(ValueError, match="unique"):
        allocate([candidate("A"), candidate("A")], D("0"), 5)


def test_budget_and_caps_hold_for_mixed_contract_steps():
    for count in range(1, 9):
        candidates = [
            candidate(
                str(index),
                rate=D(index + 1) * D("17.13"),
                step=D("0.01") * D(index + 1),
                min_cost=D("1"),
                max_cost=D("100") if index % 2 else None,
            )
            for index in range(count)
        ]
        for budget in (D("0.01"), D("1"), D("100"), D("950")):
            for slots in (1, 3, 5):
                plans = allocate(candidates, budget, slots)
                assert len(plans) <= slots
                assert sum((plan["notional"] for plan in plans), D(0)) <= budget
                for plan in plans:
                    assert plan["amount"] > 0
                    assert plan["amount"] % plan["step"] == 0
                    assert plan["notional"] == plan["amount"] * plan["rate"]
                    assert plan["notional"] <= plan["target"]
                    assert plan["notional"] >= plan["min_cost"]
                    assert plan["max_cost"] is None or plan["notional"] <= plan["max_cost"]
