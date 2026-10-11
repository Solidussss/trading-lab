# No-real-trading safety model

Trading Lab is simulation-only. It has no brokerage account, no brokerage SDK and no
order-routing code. Every "order" is a record in the simulator's own ledger, filled against
market data. The layers below make it structurally impossible to reach a brokerage, and the
test suite attacks each one (`tests/test_no_real_trading.py`).

| # | Layer | Where | What it stops |
|---|---|---|---|
| 1 | Fail-closed configuration | `config.py` | `TRADING_MODE` must be exactly `simulated`. Any variable that looks like brokerage configuration (`ALPACA_*`, `IBKR_*`, `QUESTRADE_*`, `WEALTHSIMPLE_*`, `BROKER_*`, …) aborts startup, as do proxies, malformed tokens, padded or empty values, and risk limits above hard ceilings. A `DATABASE_URL` pointing at a brokerage domain is refused. |
| 2 | Egress allowlist | `safety/endpoints.py` | The process may reach only `api.tiingo.com` (data), `api.anthropic.com` (research), its PostgreSQL host, and loopback/private addresses. Everything else is refused, including unknown hosts and public IP literals. Known brokerage and trading-venue domains (40+, incl. all major Canadian brokers) are denied first and can never be added to the allowlist. |
| 3 | Process network guard | `safety/network_guard.py` | A Python audit hook enforces layer 2 on every DNS lookup, socket connect and `http.client` connection (including the CONNECT tunnel target behind a proxy) made by any library in the process. Hosts are IDNA-normalised first, so Unicode look-alikes and zero-width characters can't disguise a destination. Audit hooks cannot be removed; production freezes the allowlist at startup. |
| 4 | Startup self-check | `safety/selfcheck.py` | Every process proves layers 1–3 at boot by actually attempting to resolve brokerage hosts and an unknown host and requiring the guard to block them. It also fails if any OS-level proxy is configured or if asyncio is not using its stock event loop (uvloop does DNS in C, bypassing audit hooks; the server installs plain `uvicorn`, not `uvicorn[standard]`). |
| 5 | Source-code rules | `tests/test_no_real_trading.py` | No brokerage SDK imports; brokerage domains appear only in the denylist; no `submit_order`/`place_order` implementation outside the documented future interface; outbound HTTP only in the vetted data client (stdlib `urllib`) and, later, the Claude client. |

## Known limits (stated plainly)

- Libraries that do networking in C (like uvloop) bypass layer 3. The self-check refuses
  uvloop; any new native networking dependency must be reviewed.
- Layer 3 matches hostnames. Private-range IP literals are allowed (Railway's internal
  network uses them for the database).
- Static checks look for known SDKs and patterns; they are a tripwire, not a proof against
  deliberate circumvention by someone editing the code.

## Future brokerage integration

`broker/base.py` documents the interface a brokerage adapter would implement. Adding one
would require removing that venue from the denylist, new safety tests, and your explicit
approval. Nothing in V1 implements it.
