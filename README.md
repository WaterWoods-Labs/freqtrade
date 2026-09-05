# WaterWoods UMX adapter for Freqtrade

[![UMX CI](https://github.com/WaterWoods-Labs/freqtrade/actions/workflows/umx-ci.yml/badge.svg?branch=umx)](https://github.com/WaterWoods-Labs/freqtrade/actions/workflows/umx-ci.yml)
[![Latest UMX release](https://img.shields.io/github/v/release/WaterWoods-Labs/freqtrade?filter=umx-*&label=UMX%20release)](https://github.com/WaterWoods-Labs/freqtrade/releases?q=umx-&expanded=true)

The `umx` branch adds a native UMX exchange adapter to the complete Freqtrade codebase.
It supports spot and USDT linear perpetual futures, with cross margin and 1x leverage for
perpetual orders. Crypto options and securities are not supported by this adapter.

## Start here

- [UMX adapter](docs/umx-adapter.md): supported behavior, configuration, API mapping, and limitations.
- [UMX maintenance](docs/umx-maintenance.md): branch ownership, upstream synchronization, CI, and releases.
- [Official Freqtrade documentation](https://www.freqtrade.io/en/stable/): installation, strategies,
  backtesting, and general operation; compare version-specific behavior with the checked-out source.
- [UMX releases](https://github.com/WaterWoods-Labs/freqtrade/releases?q=umx-&expanded=true): reviewed
  images under `ghcr.io/waterwoods-labs/freqtrade-umx`, pinned by the release's immutable digest.

## Implementation

| Component | Responsibility |
| --- | --- |
| [REST connector](freqtrade/exchange/umx_connector/client.py) | Authentication, HTTP transport, endpoint paths, and API errors. |
| [Response facade](freqtrade/exchange/umx_api.py) | Convert UMX markets, orders, balances, positions, and funding into Freqtrade/ccxt-shaped data. |
| [Exchange adapter](freqtrade/exchange/umx.py) | Register native UMX behavior and enforce trading-mode and configuration constraints. |
| Freqtrade core integration | Native exchange discovery, order-fee accounting, and position-reconciliation hooks. |
| [Software tests](tests/exchange/test_umx.py) | Adapter regression coverage, alongside the upstream compatibility suite. |

The upstream Freqtrade source, examples, and tests remain in this branch. General product
information and development documentation are maintained by the
[official Freqtrade project](https://github.com/freqtrade/freqtrade).

## Sources and repository boundaries

The private [UMX API reference repository](https://github.com/WaterWoods-Labs/team-umx-api-reference)
indexes official documentation and preserves historical XCoin evidence. It informs adapter
maintenance; neither that repository nor documentation webpages are runtime dependencies.
Current implementation contracts are maintained in the adapter guide above.

Reusable operational tooling belongs in
[team-freqtrade-runtime](https://github.com/WaterWoods-Labs/team-freqtrade-runtime).
Strategy research, backtest results, account probes, and acceptance reports belong in the
workspace's runtime-data and artifact directories. Software regression tests remain with the
code they validate and are excluded from the runtime Docker build context and source package.

[Binance Portfolio Margin/PAPI](https://github.com/WaterWoods-Labs/freqtrade/tree/binance-portfolio-margin)
is a separate product branch with its own adapter, CI, and releases.

See [support](SUPPORT.md), [security reporting](SECURITY.md), and the [GPL-3.0 license](LICENSE).
