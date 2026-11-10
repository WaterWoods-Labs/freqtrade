# Bot risk configuration v1

The optional top-level `bot_risk` contract owns bot capital, reserve, explicit pair limits,
initial/add ceilings and occupied-symbol limit. `freqtrade.bot_risk` validates finite positive
amounts and relationships. Legacy configurations without the contract retain their behavior.
The adapter advertises `bot_risk_contract_version = 1` for an exact-image capability probe.

Configured live entries require 1x PO/IOC limit orders. Under the account namespace lock the
adapter checks complete account positions/open orders, then persists an entry reservation before
submission. Unknown outcomes block subsequent entries across restart. A missing exact order is
not evidence of cancellation. Releasing a reservation requires its exact terminal order and two
flat observations at least 60 seconds apart; partial fills and unresolved submissions retain capacity.
The second entry requires a terminal filled first entry of the same direction; no third add is allowed.

UMX entry client IDs contain only 1-32 ASCII letters or digits, as required by the
[order endpoint](https://www.umx.com/docs/coin-apis/trading/regular-trading/place-order).
Generated budget IDs use `ftbr` followed by 24 hexadecimal characters. Invalid supplied IDs
are rejected before reserving capacity or sending an order. Exact-order lookup failures and
unresolved submissions use the native entry-error path: they retain the reservation and block
new entries while normal exit processing remains available. A query miss never clears an intent
or authorizes another submission.

Budgets may be lowered without changing or deleting the namespace/state. Entry-limit rejection
uses the native invalid-order path so normal exit processing remains available. Reduce-only exits
and protection bypass admission checks. Missing account evidence or an unknown submission remains
a recovery event; do not claim automatic position recovery from these software checks alone.

Each live account still has one writer, a stable namespace and shared state directory. Separate
hosts or aliases do not provide account coordination. Runtime stages in STOPPED and binds config,
assets and immutable image; paper/loader tests do not constitute live acceptance or permission to trade.
