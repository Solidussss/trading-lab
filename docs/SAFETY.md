# Paper-only safety model

The lab must never place a real-money trade. That property does not rest on any single
check; seven independent layers each block it, and the test suite attacks every layer.

| # | Layer | Where | What it stops |
|---|---|---|---|
| 1 | Fail-closed configuration | `config.py` | `TRADING_MODE` must be exactly `paper`. Missing, malformed or ambiguous settings abort startup: generic `APCA_*`/`ALPACA_API_KEY` variables, any variable with LIVE in an Alpaca name, live-format (`AK…`) or unknown-format keys, risk limits above hard ceilings, any `*_PROXY` variable in any environment, and padded or empty values (never silently trimmed or defaulted). |
| 2 | Hostname allowlist | `safety/endpoints.py` | Only `paper-api.alpaca.markets` (trading) and `data.alpaca.markets` / `stream.data.alpaca.markets` (read-only data). Live hosts are on a denylist checked first; unknown `*.alpaca.markets` hosts are refused. Disguised hosts (userinfo tricks, ports, trailing dots, case, Unicode look-alike dots, zero-width characters) are IDNA-normalised exactly as the socket layer would before checking. |
| 3 | Client factory | `broker/alpaca_paper.py` | `TradingClient` is built in exactly one place, always with `paper=True` **and** an explicit validated `url_override`. The client's effective base URL is then read back and re-validated; if it cannot be read, construction fails. |
| 4 | Pre-write re-verification | `AlpacaPaperBroker.submit_order/cancel_order` | The client's URL is re-checked immediately before every write, so tampering after construction is caught. |
| 5 | Order gate | `safety/order_gate.py` | Raises if the broker is not paper. Enforces emergency pause, symbol allowlist, long-only (no sell beyond held quantity), duplicate `client_order_id`, and halt rules. `ORDER_SUBMISSION_ENABLED=false` (the default) turns every approved order into a logged dry run. Brokers' `submit_order` requires an approving decision bound to that exact `client_order_id`. |
| 6 | Process network guard | `safety/network_guard.py` | A Python audit hook inspects every DNS lookup, socket connect and `http.client` connection (including the CONNECT tunnel target behind a proxy) in the whole process. Live or unknown Alpaca hosts raise before a packet is sent. Audit hooks cannot be removed. Alpaca HTTP sessions also have `trust_env` disabled so OS/env proxies are ignored. |
| 7 | Startup self-check | `safety/selfcheck.py` | Every process proves layers 1, 2 and 6 at boot by actually attempting to resolve each live host and requiring the guard to block it. It also fails if any OS-level proxy is configured or if asyncio is not using its stock event loop (uvloop bypasses audit hooks; the server deliberately installs plain `uvicorn`, not `uvicorn[standard]`). Failure exits non-zero. |

Plus static checks in the test suite: no `paper=False` anywhere, live hostnames only in the
denylist, `TradingClient` only in the adapter, `url_override` only in the two vetted adapters,
no legacy `alpaca_trade_api` or Broker API SDKs, no committed `.env` or key-shaped strings.

## Proof

`tests/test_live_trading_impossible.py` is organised by attack route (configuration, client
factory, adapter tampering, order gate, network, source code). Run it with:

```
pytest tests/test_live_trading_impossible.py -v
```

## Independent audit

Before delivery, a separate reviewer that had not seen the implementation audited this layer
and the backtester. It found no live-trading path in the primary flow, and eleven weaknesses
in backup layers and research integrity (Unicode look-alike hosts, proxies, uvloop,
drawdown-halt reset, test-vault aliasing, partial bars, gap-up exposure, gate bypass, lax
parsing, secret pickling, narrow source scans). All were fixed and each has a regression test.

## Known limits (stated plainly)

- The network guard matches hostnames. Code that resolved a live host's IP elsewhere and
  connected to the raw IP would not be caught by layer 6; layers 1–5 still apply, and the
  source-code checks forbid live hostnames in the codebase.
- Code that uses a library doing DNS/sockets in C (as uvloop does) bypasses layer 6. The
  self-check refuses uvloop, but any new native networking dependency must be reviewed.
- The gate-approval requirement stops accidental bypass, not deliberate forgery: code inside
  this process could construct an approving decision. Layers 1–4 and 6 still confine any
  order to the paper endpoint.
- Key-prefix checks (`PK` paper / `AK` live) reflect Alpaca's current key format. If Alpaca
  changes formats, startup fails closed with a clear message rather than guessing.
- With `ORDER_SUBMISSION_ENABLED=true`, orders go to the **paper** endpoint. Enabling it is
  a deliberate, separate step that requires your explicit approval.

## Activation checklist (before `ORDER_SUBMISSION_ENABLED=true`)

1. Full test suite green in CI on Python 3.12 with real dependencies.
2. Self-check passes on Railway with the production environment.
3. At least five sessions of dry-run decisions reviewed on the dashboard.
4. Broker reconciliation shows zero unexplained differences.
5. Your explicit written approval.
