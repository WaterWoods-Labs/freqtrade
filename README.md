# WaterWoods Binance Portfolio Margin

Binance Portfolio Margin (PAPI) support for Freqtrade, maintained on the
`binance-portfolio-margin` branch of `WaterWoods-Labs/freqtrade`.
This repository retains the full Freqtrade source tree and its regression tests.

The adapter supports standard Portfolio Margin accounts trading linear USD-M perpetuals in
cross-margin, one-way mode. It adds PAPI routing, account and order mapping, persistent order
recovery, and account reconciliation. With Portfolio Margin disabled, ordinary Binance behavior
uses the existing Freqtrade paths.

## Start here

- [Adapter contract and configuration](docs/binance-portfolio-margin-adapter.md): supported
  capabilities, configuration fragment, routing, and restrictions.
- [Source maintenance](docs/binance-portfolio-margin-maintenance.md): branch boundaries,
  upstream synchronization, CI, and releases.
- [Shared runtime runbook](https://github.com/WaterWoods-Labs/team-freqtrade-runtime/blob/main/docs/binance-portfolio-margin-runbook.md):
  deployment, account acceptance, strategy operation, and recovery.
- [Official Freqtrade documentation](https://www.freqtrade.io/en/stable/): installation,
  general configuration, strategies, and upstream features.

## Source map

| Responsibility | Source |
| --- | --- |
| Binance/PAPI integration and CCXT routing | [binance.py](freqtrade/exchange/binance.py) |
| Persistent order intents and entry reservations | [binance_order_intent.py](freqtrade/exchange/binance_order_intent.py) |
| Shared Chan policy validation | [binance_portfolio_policy.py](freqtrade/exchange/binance_portfolio_policy.py) |
| RPC entry gate | [rpc.py](freqtrade/rpc/rpc.py) |
| Exchange-neutral reconciliation hook | [exchange.py](freqtrade/exchange/exchange.py), [freqtradebot.py](freqtrade/freqtradebot.py) |
| Software regression tests | [tests/](tests/) |

Strategy backtests, account probes, live acceptance reports, and local runtime data belong in
the workspace runtime/artifact directories described by the shared runbook. They are not product
documentation or release inputs. Upstream examples and regression fixtures remain in this tree.

## Sources and related product

- [Binance Portfolio Margin API](https://developers.binance.com/en/docs/products/derivatives-trading-portfolio-margin/general-info)
- [CCXT](https://github.com/ccxt/ccxt) and [official Freqtrade](https://github.com/freqtrade/freqtrade)
- [UMX adapter](https://github.com/WaterWoods-Labs/freqtrade/tree/umx), maintained independently

Freqtrade and this derivative are distributed under [GPL-3.0](LICENSE).
