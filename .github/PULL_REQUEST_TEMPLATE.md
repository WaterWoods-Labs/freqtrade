## Summary

<!-- Explain the goal and why it belongs in the WaterWoods XCoin fork. -->

Solve the issue: #___

## Target and scope

- [ ] This PR targets `xcoin` (never `develop` or `stable`).
- [ ] XCoin spot
- [ ] XCoin perpetual futures
- [ ] Freqtrade core integration
- [ ] CI, synchronization, release, or documentation only

## Validation

- [ ] Ran the focused XCoin tests: `pytest tests/exchange/test_xcoin.py`
- [ ] Ran all tests affected by the change.
- [ ] Ran pre-commit or equivalent lint/type checks.
- [ ] Added or updated regression tests where behavior changed.

## Runtime evidence

- Dry-run mode, pair, timeframe, and duration:
- Observed result:
- Full upstream test suite required: yes / no

## Trading and security risk

- [ ] No exchange keys, API credentials, account identifiers, private order data, or raw live logs are included.
- [ ] Live trading remains explicitly gated by `xcoin_live_trading_enabled=true`.
- [ ] Futures margin, leverage, position reconciliation, fee, and quantity-conversion impacts were reviewed where relevant.
- Rollback notes:

## AI-assisted changes

- AI assistance used: yes / no
- Human review performed:
