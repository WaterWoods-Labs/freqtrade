# Binance Portfolio Margin adapter

This page defines the PAPI adaptation contract. Source and release rules are in
[maintenance](binance-portfolio-margin-maintenance.md); deployment, strategy procedures, account
acceptance, and recovery are in the
[shared runtime runbook](https://github.com/WaterWoods-Labs/team-freqtrade-runtime/blob/main/docs/binance-portfolio-margin-runbook.md).

## Supported scope

The separate `chan_dca4_235_2000_v1` policy admits the reviewed 2026-10-28 catalog
of 235 USDT-quoted and USDT-settled contracts. It retains the 2000 USDT aggregate,
200 USDT per-symbol, 100 USDT initial/add, 1x leverage and 20 occupied-symbol limits.
Short price histories do not remove an admitted symbol; the strategy owns its
unchanged signal warmup. The original 56-symbol policies retain their exact scope.

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

The reviewed dependency pin is CCXT 4.5.85. Offline fake-transport regression tests retain an
exact version check alongside private order/Algo routing, retry suppression, public market loading,
and single-symbol/bulk public quote checks. These checks exercise the installed CCXT signing and
request construction without exchange access. A later dependency upgrade requires reviewing and
validating these contracts again.

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
| `chan_dca4_56_v1` | Frozen 56-pair DCA4 universe and matching whitelist; long/short, 1x, initial entry at most 40 USDT, one confirmed same-side add at most 35 USDT, pair cap 75 USDT, aggregate cap 950 USDT and at most 20 occupied pairs. Force-entry remains disabled. |
| `chan_dca4_56_2000_v1` | Same 56-pair universe, lease, recovery and protection rules; initial entry and the single add are each at most 100 USDT, pair cap 200 USDT, aggregate cap 2000 USDT, 1x and at most 20 occupied pairs. The original policy retains its original limits. |

The DCA policy additionally requires an entry lease from the reviewed strategy router. It binds
pair, side, exact price, PO/IOC routing and the closed candle's 60-second signal window. Both
wall-clock and monotonic deadlines are checked at CCXT signing, after throttling and before HTTP.
Expired leases cannot be retried or reused. Protective reduce-only exits remain available.

An add requires a terminal, filled first entry, observed same-direction position, no pending
entry and no reduction of the original quantity. Durable reservations survive restarts; exact
order evidence reserves confirmed or pending notional, while missing evidence retains the full
pair cap. The legacy five-pair policy retains its original one-reservation-per-pair limits.

Observed Maker partial fills are settled through `settle_portfolio_dca_partial_entry`: the
reservation is atomically converted into a pending intent before cancelling the remainder.
Only exact terminal order evidence permits normal continuation. Ambiguous cancellation uses the
existing late-fill containment and leaves trading latched stopped. A crash during cancellation
therefore cannot erase the uncertain entry. The runtime owns fixed-anchor stop replacement and
actual-fee accounting; these adapter tests alone do not certify a complete live strategy.

Chan entries must be limit orders. Before submission, projected positions, open orders, and local
reservations are checked against the configured entry limits. Reduce-only exits are not blocked
by entry-policy limits. These caps constrain requested entries and reservations; they do not bound
fill notional, mark-to-market exposure, fees, slippage, or losses. The 1x constraint belongs to
these policies and is not a claim about all Binance Portfolio Margin accounts.

Policy deployment examples, strategy backtests, account probe results, and live acceptance reports
are maintained with the shared runtime and workspace artifacts, not on this adapter page.

## Sources

- [Binance Portfolio Margin API](https://developers.binance.com/en/docs/products/derivatives-trading-portfolio-margin/general-info)
- [CCXT Binance implementation](https://github.com/ccxt/ccxt/blob/v4.5.85/python/ccxt/binance.py)
- [Freqtrade exchange documentation](https://www.freqtrade.io/en/stable/exchanges/)
- [Product source](https://github.com/WaterWoods-Labs/freqtrade/tree/binance-portfolio-margin/freqtrade/exchange)

API documentation describes the exchange surface. Supported behavior for this product is the
narrower contract above, implemented by the product branch and covered by its regression tests.
