# Security Policy

## Supported versions

WaterWoods security fixes are maintained and released independently for each product:

| Product | Supported branch | Immutable releases |
| --- | --- | --- |
| UMX | `umx` | `umx-YYYY.MM.DD.N` |
| Binance Portfolio Margin | `binance-portfolio-margin` | `binance-portfolio-margin-YYYY.MM.DD.N` |

The `develop` and `stable` branches are clean upstream mirrors and do not contain either
WaterWoods product integration. Never use one product's image, configuration, or credentials to
reproduce a vulnerability in the other product.

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/WaterWoods-Labs/freqtrade/security/advisories/new).
Do not open a public issue for a vulnerability and never include exchange credentials, signing
secrets, tokens, private account data, or exploitable details in a public discussion.

If a credential may have been exposed, revoke or rotate it immediately and review account activity.
Deleting a file or commit is not a substitute for rotating a compromised credential.
