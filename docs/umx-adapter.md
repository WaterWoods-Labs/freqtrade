# UMX adapter

This guide describes the implemented UMX adapter contract. It translates UMX REST APIs into
Freqtrade's native exchange interface; it is not a copy of the exchange's API manual.
Branch, CI, and release procedures are in [UMX maintenance](umx-maintenance.md).

Maker-specific options are opt-in: `exchange.umx_strict_ohlcv=true` retains a last candle
only when its closing time has passed and disables missing-candle filling;
`exchange.umx_leverage_readonly=true` checks that symbol leverage is already 1x without
setting it; `exchange.umx_public_request_interval=0.2` spaces public GET starts across
the synchronous/asynchronous clients in one process. Private requests bypass that public
queue so account/exit operations do not sit behind warmup reads. This is not a shared
IP-wide limiter across processes. A filled minimum whole contract permits cancellation
of its unfilled remainder without applying the extra entry reserve. These changes do
not by themselves guarantee timely signals or prove live fills. Version 3 also adds the
position-bound stop transport described below; its live exchange acceptance is pending.
`exchange.umx_entry_stoploss=-0.03` attaches a last-price market stop to every non-reduce-only
PO perpetual entry. `exchange.umx_order_read_only=true` blocks every non-GET request before
HTTP transport. Unapproved `UMXChanB1S1Maker` configurations force that read-only setting after
generic client overrides; they may be deployed STOPPED without enabling account writes.

## Supported integration

| Capability | Adapter behavior |
| --- | --- |
| Spot | `trading_mode=spot`; unleveraged orders, for example `BTC/USDT`. |
| Linear perpetual futures | `trading_mode=futures`, `margin_mode=cross`; USDT settlement, for example `BTC/USDT:USDT`; 1x leverage only. |
| Orders | Limit and market; limit time-in-force supports GTC, IOC, FOK, and PO. PO maps to native `post_only` with GTC; market orders use IOC. |
| Exchange stop candidate | Last-price TPSL market stops for existing net perpetual positions; software-tested, live acceptance pending. |
| Market data | REST markets, tickers, order books, and candles; perpetual mark/index candles and funding rates. |
| Account data | Balances, orders, fills, perpetual positions, leverage, and settled funding bills. |
| Unsupported | Crypto options, securities, isolated margin, leveraged spot, spot stoploss, and WebSocket subscriptions. |

Native UMX must remain discoverable through `list-exchanges`, the web API exchange list, and
`new-config`. It is not registered as a ccxt-provided exchange. Documentation coverage and
account/API-key permissions do not extend the adapter's implemented capabilities.

## Configuration

Merge the following fragment into a normal Freqtrade configuration for perpetual dry-run use:

```json
{
  "dry_run": true,
  "trading_mode": "futures",
  "margin_mode": "cross",
  "stake_currency": "USDT",
  "exchange": {
    "name": "umx",
    "umx_live_trading_enabled": false,
    "umx_timeout": 10,
    "pair_whitelist": ["BTC/USDT:USDT"]
  }
}
```

For spot, use `trading_mode=spot`, omit `margin_mode`, and use spot pair names such as `BTC/USDT`.
The fragment supplies adapter settings only; the usual strategy, pricing, and other Freqtrade
settings are still required.

| Setting | Contract |
| --- | --- |
| `exchange.name` | Only `umx`; the removed `xcoin` selector and `xcoin_*` options fail explicitly. |
| `exchange.umx_timeout` | REST request timeout in seconds; default `10`. |
| `exchange.account_name` / `exchange.accountName` | Optional account routing; falls back to `UMX_ACCOUNT_NAME` and is sent as `accountName`. |
| API credentials | Read from `FREQTRADE__EXCHANGE__KEY` / `FREQTRADE__EXCHANGE__SECRET`, with per-field fallbacks to `UMX_API_KEY` / `UMX_API_SECRET`. |
| Live trading | Requires both `dry_run=false` and `exchange.umx_live_trading_enabled=true`, plus environment-provided credentials. |
| REST host | Fixed to `https://api.umx.com/api`; custom and legacy hosts are rejected. |

Keep credentials in the runtime environment. The adapter does not use inline configuration
credentials as a substitute for its environment credential contract.

## API mapping

The [REST connector](https://github.com/WaterWoods-Labs/freqtrade/blob/umx/freqtrade/exchange/umx_connector/client.py)
owns signing, transport, endpoint paths, and error mapping. The
[response facade](https://github.com/WaterWoods-Labs/freqtrade/blob/umx/freqtrade/exchange/umx_api.py)
converts API payloads; the
[exchange subclass](https://github.com/WaterWoods-Labs/freqtrade/blob/umx/freqtrade/exchange/umx.py)
supplies Freqtrade capabilities and mode-specific behavior.

| Freqtrade operation | UMX endpoint |
| --- | --- |
| Load markets | `GET /v2/public/symbols` |
| Tickers and order book | `GET /v1/market/ticker/mini`, `/v1/market/ticker/24hr`, `/v1/market/depth` |
| Candles | `GET /v1/market/kline`, `/v1/market/markPriceKline`, `/v1/market/indexPriceKline` |
| Balances and positions | `GET /v1/account/balance`, `/v2/trade/positions` |
| Create / cancel order | `POST /v2/trade/order`, `/v1/trade/cancelOrder` |
| Query orders and fills | `GET /v2/trade/order/info`, `/v2/trade/openOrders`, `/v2/history/trades` |
| Position-bound market stop | `POST /v2/trade/stopPosition` |
| Query / cancel TPSL | `GET /v2/trade/openOrderComplex`, `/v2/history/orderComplexs`; `POST /v1/trade/cancelComplex` |
| Set / read leverage | `POST` / `GET /v1/trade/lever` |
| Current / historical funding rate | `GET /v1/market/fundingRate`, `/v1/market/fundingRate/history` |
| Settled funding | `GET /v1/history/bill` with `actionType=18` |

Symbol conversion maps `BTC-USDT` to `BTC/USDT` and `BTC-USDT-PERP` to `BTC/USDT:USDT`.
Futures quantities are converted between contracts and coin amounts using `ctVal`.
Wire fields including `businessType`, `accountName`, `role`, and `lever` retain their API names.

## Behavioral constraints

- Spot orders always send `isLeverage=false`. Perpetual orders omit this spot-only field and route
  through their contract symbol. Opening orders set symbol-level leverage to 1x; every perpetual
  order requires a successful symbol-scoped 1x readback. Requests for other leverage are rejected.
- A live futures position must agree with the database's pair, direction, and amount. The
  reconciliation hook blocks trading when those records conflict.
- Futures `totalEquity` includes unrealized PnL. The adapter declares this to Freqtrade Wallets,
  which removes UPL once before exposing the balance total. Order-fee accounting preserves signed
  costs and rebates; these core integration changes are part of the adapter's behavior.
- Batch mini tickers have no bid/ask, so the adapter does not advertise batch spread support or
  substitute the last price for either side. Single-symbol tickers obtain genuine bid/ask from depth.
- Settled funding uses private bills. Public rate history supports dry-run calculations; its `1h`
  timeframe is a download-shard width, not a fixed settlement interval. Each request stays within
  its shard; current rates expose the symbol's `fundingTime` and hour-based `fundingInterval`.
- Dry-run liquidation prices use an approximation based on wallet collateral and `riskEngineRate`;
  unavailable inputs can produce `None`. This is simulation compatibility, not an exchange quote.
- The REST connector submits an order once and does not automatically retry an uncertain write.
- The Maker version 3 candidate supports last-price market stops on existing net perpetual
  positions. It checks direction and quantity, adopts a matching owned TPSL after an uncertain
  response, and reads back accepted parameters. A triggered TPSL resolves to its execution order;
  trigger status alone never supplies a fill. Cancellation reads back the resulting state.
  Spot, take-profit and limit-stop variants remain unsupported. Full live TPSL acceptance is
  still pending. Attached entry stops are sent atomically with PO entries
  when configured; adoption requires the actual position quantity and an equal or tighter trigger.
  Venue activation during partial fills and remainder cancellation still require live evidence.
- Observed attached stops (`ftsa` client IDs) use `untrigger` while waiting, and report the
  entry direction in their raw `side`. The stop adapter accepts `untrigger` and legacy `live`
  as open/cancellable states. It matches an attached stop using that entry direction and exposes
  the opposite, closing direction to Freqtrade while retaining the raw response in `info`.
  Independently placed position stops (`ftsl` client IDs) keep their existing closing-direction
  contract; the attached-order observations do not establish a different wire format for them.
  Unknown states are rejected with the raw state value in the error. Live evidence
  (2026-09-16, SOXL-USDT-PERP attached stop) shows a triggered attached stop reports `filled`
  with no execution-order reference, and `order_info` cannot resolve its `ftsa` client ID;
  the adapter reconciles the execution order strictly from bounded post-trigger fills
  (single market order matching the stop quantity) and rejects ambiguity. The simulated
  `slEffective` response remains protocol test coverage for the legacy trigger wording.

## Documentation sources

Consult the official [concise coin API](https://www.umx.com/zh-CN/docs/coin-apis/introduction/quick-start)
and [full coin API](https://www.umx.com/zh-CN/docs/coin-api/introduction/quick-start) when changing
endpoint paths or parsing. The private
[source registry](https://github.com/WaterWoods-Labs/team-umx-api-reference/blob/main/umx-source-registry.json)
records document URLs and change fingerprints, not a guarantee that every documented product is
implemented here. Check the current official contract; older XCoin snapshots are dated evidence
for gaps in current documentation, not proof of current behavior.

The reference repository is a development source only. Installing or running this adapter does
not require its checkout or access to documentation webpages.
