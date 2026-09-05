# UMX adapter

This guide describes the implemented UMX adapter contract. It translates UMX REST APIs into
Freqtrade's native exchange interface; it is not a copy of the exchange's API manual.
Branch, CI, and release procedures are in [UMX maintenance](umx-maintenance.md).

## Supported integration

| Capability | Adapter behavior |
| --- | --- |
| Spot | `trading_mode=spot`; unleveraged orders, for example `BTC/USDT`. |
| Linear perpetual futures | `trading_mode=futures`, `margin_mode=cross`; USDT settlement, for example `BTC/USDT:USDT`; 1x leverage only. |
| Orders | Limit and market; limit time-in-force supports GTC, IOC, and FOK. Market orders use IOC. |
| Market data | REST markets, tickers, order books, and candles; perpetual mark/index candles and funding rates. |
| Account data | Balances, orders, fills, perpetual positions, leverage, and settled funding bills. |
| Unsupported | Crypto options, securities, isolated margin, leveraged spot, exchange-hosted stoploss, and WebSocket subscriptions. |

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
