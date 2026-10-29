# Bot risk configuration v1

The optional top-level `bot_risk` contract owns bot capital, reserve, explicit pair limits,
initial/add ceilings and occupied-symbol limit. `freqtrade.bot_risk` validates finite positive
amounts and relationships. The `bot_config_v1` Portfolio Margin policy must exactly equal
`portfolio_policy(bot_risk)`; contradictory aliases fail before constructing the exchange.
No adapter business constant or image rebuild is needed to choose another valid amount or pair set.

Legacy policies retain their limits and behavior. Contract v1 uses the same 1x, PO/IOC entry lease,
account lock, durable intent/reservation, partial fill and unknown-order containment mechanisms.
Dynamic limits count positions, pending orders and unresolved reservations; budget rejections do
not disable reduce-only exits. Lower limits do not erase account recovery state. The namespace and
shared user_data identity must remain stable, and unresolved pairs cannot be removed.

The adapter advertises `bot_risk_contract_version = 1`. Runtime must probe that capability on the
exact immutable image and validate complete generated configuration before staging STOPPED.
Source edits do not create a released image or authorize account operations. Tests cover config
relationships and submission admission; live acceptance follows the runtime runbook independently.
