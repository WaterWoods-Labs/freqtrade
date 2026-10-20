"""Pure, ordered opportunity allocation; amounts and steps are base-asset quantities."""

from decimal import ROUND_DOWN, Decimal, localcontext


_ZERO = Decimal(0)
_LIMITS = ("min_amount", "max_amount", "min_cost", "max_cost")


def _number(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return value


def _amount(target: Decimal, candidate: dict) -> Decimal:
    # Divide by the full base-quantity increment, never by ctVal a second time.
    return (target // (candidate["rate"] * candidate["step"])) * candidate["step"]


def _meets_minimum(amount: Decimal, candidate: dict) -> bool:
    return (
        amount > 0
        and (candidate.get("min_amount") is None or amount >= candidate["min_amount"])
        and (
            candidate.get("min_cost") is None or amount * candidate["rate"] >= candidate["min_cost"]
        )
    )


def allocate(candidates: list[dict], available_budget: Decimal, free_slots: int) -> list[dict]:
    """Freeze equal-dollar allocations in supplied priority order.

    Callers provide only currently eligible candidates and an already reconciled
    remaining budget. ``step`` is ctVal times the contract increment; every amount
    limit is also in base units. Numeric inputs must be finite Decimals.

    Market maxima redistribute their excess among selected uncapped candidates.
    A rounded minimum failure permanently removes only the last failing candidate
    in priority order, then later candidates backfill and the whole plan restarts.
    Quantity-rounding dust is never redistributed. No input is mutated, and this
    function has no access to fills, prices, wallets, or execution state.
    """
    budget = _number(available_budget, "available_budget")
    if type(free_slots) is not int or free_slots < 0:
        raise ValueError("free_slots must be a nonnegative integer")
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")

    numbers = [budget]
    pairs: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Each candidate must be a dictionary")
        pair = candidate.get("pair")
        if not isinstance(pair, str) or not pair.strip() or pair in pairs:
            raise ValueError("Candidate pairs must be nonempty and unique")
        pairs.add(pair)
        for key in ("rate", "step"):
            numbers.append(_number(candidate.get(key), key, positive=True))
        for key in _LIMITS:
            if candidate.get(key) is not None:
                numbers.append(_number(candidate[key], key))

    if not candidates or budget == 0 or free_slots == 0:
        return []

    # Keep product/floor operations exact even under a caller's low precision.
    # Round division down so an inexact equal share can never overdraw the budget.
    precision = max(
        28,
        4
        * max(
            len(value.as_tuple().digits) + abs(int(value.as_tuple().exponent)) for value in numbers
        )
        + len(str(len(candidates)))
        + 8,
    )
    with localcontext() as context:
        context.prec = precision
        context.rounding = ROUND_DOWN
        return _allocate(candidates, budget, free_slots)


def _capacity(candidates: list[dict], budget: Decimal) -> tuple[dict[str, Decimal], set[str]]:
    caps: dict[str, Decimal] = {}
    rejected: set[str] = set()
    for candidate in candidates:
        pair, rate, step = candidate["pair"], candidate["rate"], candidate["step"]
        maxima = []
        if candidate.get("max_amount") is not None:
            maxima.append((candidate["max_amount"] // step) * step * rate)
        if candidate.get("max_cost") is not None:
            maxima.append(_amount(candidate["max_cost"], candidate) * rate)
        if maxima:
            caps[pair] = min(maxima)
        if not _meets_minimum(_amount(min(budget, caps.get(pair, budget)), candidate), candidate):
            rejected.add(pair)
    return caps, rejected


def _allocate(candidates: list[dict], budget: Decimal, free_slots: int) -> list[dict]:
    caps, rejected = _capacity(candidates, budget)

    while True:
        chosen = [candidate for candidate in candidates if candidate["pair"] not in rejected][
            :free_slots
        ]
        if not chosen:
            return []
        remaining, active = budget, chosen.copy()
        targets: dict[str, Decimal] = {}
        while active:
            share = remaining / len(active)
            capped = [c for c in active if c["pair"] in caps and caps[c["pair"]] <= share]
            if not capped:
                targets.update({c["pair"]: share for c in active})
                break
            for candidate in capped:
                pair = candidate["pair"]
                targets[pair] = caps[pair]
                remaining -= caps[pair]
            active = [candidate for candidate in active if candidate["pair"] not in targets]

        proposed, failures = [], []
        for candidate in chosen:
            pair = candidate["pair"]
            amount = _amount(targets[pair], candidate)
            proposed.append(
                {
                    **candidate,
                    "amount": amount,
                    "notional": amount * candidate["rate"],
                    "target": targets[pair],
                }
            )
            if not _meets_minimum(amount, candidate):
                failures.append(pair)
        if not failures:
            if sum((plan["notional"] for plan in proposed), _ZERO) > budget:
                raise ValueError("Opportunity allocations exceeded the available budget")
            return proposed
        rejected.add(failures[-1])
