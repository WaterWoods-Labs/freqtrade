## Summary

<!-- Explain the goal and why it belongs in one WaterWoods product branch. -->

Solve the issue: #___

## Product target and scope

Select exactly one product target. Product changes never target the clean `develop` or `stable`
mirrors and never merge one product branch into the other.

- [ ] This PR targets `umx`.
- [ ] This PR targets `binance-portfolio-margin`.
- [ ] This PR does not target `develop` or `stable`.
- [ ] UMX spot
- [ ] UMX perpetual futures
- [ ] Binance standard Portfolio Margin/PAPI
- [ ] Freqtrade core integration
- [ ] CI, synchronization, release, or documentation only

## Validation

- [ ] Ran the relevant product-focused tests (`tests/exchange/test_umx.py` for UMX, or the
      Binance Portfolio Margin boundary/configuration/Binance/bot/RPC selection for that product).
- [ ] Ran all tests affected by the change.
- [ ] Ran pre-commit or equivalent lint/type checks.
- [ ] Added or updated regression tests where behavior changed.

## Runtime evidence

- Dry-run mode, pair, timeframe, and duration:
- Observed result:
- Full upstream test suite required: yes / no

## Trading and security risk

- [ ] No exchange keys, API credentials, account identifiers, private order data, or raw live logs are included.
- [ ] UMX live trading remains explicitly gated by `umx_live_trading_enabled=true`, when relevant.
- [ ] Binance Portfolio Margin remains limited to its documented account and risk policy, when relevant.
- [ ] Futures margin, leverage, position reconciliation, fee, and quantity-conversion impacts were reviewed where relevant.
- Rollback notes:

## AI-assisted changes

- AI assistance used: yes / no
- Human review performed:
