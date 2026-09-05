# Binance Portfolio Margin adapter

This page defines the PAPI adaptation contract. Source and release rules are in
[maintenance](binance-portfolio-margin-maintenance.md); deployment, strategy procedures, account
acceptance, and recovery are in the
[shared runtime runbook](https://github.com/WaterWoods-Labs/team-freqtrade-runtime/blob/main/docs/binance-portfolio-margin-runbook.md).

## Supported scope

The adapter extends Freqtrade's existing `Binance` exchange through CCXT. It supports standard
Portfolio Margin accounts, linear USD-M perpetuals, `futures` trading, `cross` margin, and one-way
positions. Spot/margin trading, inverse/COIN-M trading, hedge mode, Portfolio Margin Pro, and
PAPIv2 routing are outside this contract. Read-only COIN-M and margin checks detect incompatible
account exposure; they do not enable those markets for trading.

With PAPI disabled, ordinary Binance behavior follows the existing Freqtrade implementation.
Dry-run uses Freqtrade simulation and does not establish that a real account meets PAPI requirements.

## Configuration

Merge this fragment into a normal Freqtrade configuration:

```json
{
  "dry_run": true,
  "trading_mode": "futures",
  "margin_mode": "cross",
  "exchange": {
    "name": "binance",
    "ccxt_config": {
      "options": {
        "portfolioMargin": true
      }
    }
  }
}
```

The explicit Boolean `exchange.ccxt_config.options.portfolioMargin=true` selects PAPI; there is
no separate exchange name. Enabling it only through synchronous/asynchronous overrides or private
method parameters is rejected. Normal Freqtrade configuration, pair selection, and credentials
remain necessary; this fragment is not a complete launch configuration. A live configuration
requires an absolute `user_data_dir` for persistent order state.

The adapter supplies linear market selection, `fetchPositions.method=positionRisk`,
`fetchCurrencies=false`, `useV2=false`, and a 5000 ms request timeout. Explicit timeouts must be
positive and at most 5000 ms. Overrides that disable PAPI, select another account/subtype, or
enable transport retries are rejected. Keep these defaults instead of duplicating routing options
across `ccxt_config`, `ccxt_sync_config`, and `ccxt_async_config`.

## Interface mapping

| Freqtrade responsibility | PAPI adaptation |
| --- | --- |
| Account validation | `papiGetUmPositionSideDual` requires one-way mode; `papiGetUmAccountConfig` requires `canTrade` for live use. |
| Normal orders, fills, positions, funding, and leverage | CCXT unified methods with PAPI, linear, and cross-margin parameters; routing cannot fall back to ordinary FAPI. |
| Conditional/Algo orders | A narrow CCXT `request` shim uses the `papi` namespace for `um/algo/order`, `um/algo/algoOrder`, `um/algo/openAlgoOrders`, and `um/algo/allAlgoOrders`. |
| Balances | CCXT balance mapping; when USDT free balance is non-positive, `papiGetBalance` supplies `crossMarginFree + min(umWalletBalance, 0)` and locked collateral. Positive UM balances retain the normal mapping. |
| Existing positions and orders | Reconcile exchange exposure with the local trade database and pending orders; reject unsupported COIN-M/margin exposure. |

The Algo shim keeps CCXT signing, time synchronization, HTTP transport, error handling, and rate
limits. It validates the supported methods and fields because the pinned CCXT implementation does
not provide all required Portfolio Margin Algo methods. It is not a second HTTP/signing client.

## Order recovery and reconciliation

Persistent order intents distinguish a failed request from an unknown exchange result. The adapter
recovers by client order ID and exchange evidence before permitting another write; ambiguous or
corrupt state blocks further trading rather than blindly resubmitting. Order intents, locks, and
entry reservations are runtime functionality, not stored test results.

`Exchange.validate_existing_positions` is an exchange-neutral, default no-op hook. Freqtrade calls
it after startup order recovery and wallet refresh, and after a completed exit only when no
remaining trade has a pending order. PAPI reconciliation stays in the Binance override; generic
core call sites do not contain exchange routing or account-specific checks.

The Chan policy serializes account snapshots, order submission, and persistent entry reservations
using a shared account namespace and local cross-process lock. Writers for the same account must
share the same `user_data_dir` and stable, non-secret `account_namespace`. The namespace is 8–64
characters, starts with a letter or digit, and otherwise permits letters, digits, `_`, and `-`;
state filenames use its hash. Local locks cannot control manual orders or writers on another host.
The runbook defines the corresponding account and writer deployment requirements and recovery steps.

## Entry policy constraints

`exchange.portfolio_margin_risk` is optional for the adapter, but RPC force-entry requires an
explicit supported policy. The adapter and RPC share Chan validation in
`freqtrade/exchange/binance_portfolio_policy.py`; the policy is not a public extension API.

| Existing policy | Enforced behavior |
| --- | --- |
| Single-pair canary | Exact supported fields; one whitelisted pair, long side, 1x leverage, positive entry notional at most 100 USDT, market force-entry, and rejection of a supplied force-entry price. |
| `chan_multi_pair` | Exact five-pair set: BTC, ETH, BNB, SOL, and SPY as `/USDT:USDT`; matching whitelist; long and short sides; 1x leverage; positive per-pair limits at most 100 USDT and total at most 500 USDT; valid account namespace; force-entry disabled and supplied force-entry prices rejected. |

Chan entries must be limit orders. Before submission, projected positions, open orders, and local
reservations are checked against the configured entry limits. Reduce-only exits are not blocked
by entry-policy limits. These caps constrain requested entries and reservations; they do not bound
fill notional, mark-to-market exposure, fees, slippage, or losses. The 1x constraint belongs to
these policies and is not a claim about all Binance Portfolio Margin accounts.

Policy deployment examples, strategy backtests, account probe results, and live acceptance reports
are maintained with the shared runtime and workspace artifacts, not on this adapter page.

## Sources

- [Binance Portfolio Margin API](https://developers.binance.com/en/docs/products/derivatives-trading-portfolio-margin/general-info)
- [CCXT Binance implementation](https://github.com/ccxt/ccxt/blob/v4.5.76/python/ccxt/binance.py)
- [Freqtrade exchange documentation](https://www.freqtrade.io/en/stable/exchanges/)
- [Product source](https://github.com/WaterWoods-Labs/freqtrade/tree/binance-portfolio-margin/freqtrade/exchange)

API documentation describes the exchange surface. Supported behavior for this product is the
narrower contract above, implemented by the product branch and covered by its regression tests.
