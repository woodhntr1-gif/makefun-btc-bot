# makefun-btc-bot

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/woodhntr1-gif/makefun-btc-bot/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/woodhntr1-gif/makefun-btc-bot/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)

A mean-reversion trading bot for Coinbase Wrapped BTC (cbBTC), built by an AI agent for make.fun's [Race a BTC trading bot live for 24 hours](https://make.fun/bounty/btc-race-24h-20261117-0603) bounty, $250 in USDC. It trades $1,000 of paper money on 1-minute on-chain prices: it buys dips below the 90-minute average with freefall and drawdown brakes, and exits on take-profit, recovery, or stop-loss.

## Run it

```sh
python3 -P -s main.py
```

The bot speaks the agent-fun/bars-replay/v1 protocol: NDJSON on stdin/stdout, one reply per batch.

---

# cbBTC Dip-Reversion Bot — make.fun "Race a BTC trading bot live for 24 hours"

Bounty: https://make.fun/bounty/btc-race-24h-20261117-0603 · slug `btc-race-24h-20261117-0603` · $250 prize pool, free entry, paper money only.
Deadline: **Tue 17 Nov 2026, 12:03 UTC (19:03 WIB)**. Deliverable: `main.py` at repo root (Python 3.13, stdlib only, no network / wall clock / files / env — none are used).

## Local dev grade (the gate that matters)

| Window | Net PnL | Trades | Max drawdown | Fees |
|---|---|---|---|---|
| **Real frozen dev window (kit dataset, 1,440 bars)** | **+$6.19** | 8 | 0.45% | $1.61 |
| Synthetic uptrend (+~9% drift) | +$15.86 | 10 | 0.49% | $2.02 |
| Synthetic downtrend (−~9% drift) | −$8.51 | 16 | 2.25% | $3.19 |
| Synthetic sideways | +$9.86 | 10 | 0.77% | $2.01 |
| Synthetic crash (−11% in 100 min mid-window) | −$0.04 | 12 | 1.68% | $2.40 |

Dev gate is net PnL > $0 after fees → **passed locally (+$6.19, +0.62%)**. Race guardrails (≥1 trade, max DD ≤ 25%) are met with a wide margin in every scenario. Determinism verified: two full runs over the dev window produce byte-identical reply streams.

**How graded:** the kit's `grade-local --driver process` runs Node bots only (Python needs Docker, unavailable in this environment), so `test_harness.py` (in this folder, *not* part of the entry) re-implements the fill model exactly as written in the kit's own harness source (`harness/sim/bars/engine.ts`: buy spend = notional·(1−4 bps), price = next open ·(1+2 bps)·(1+spend/D); sell mirrored; max-slippage vs previous close; flatten at last close vs `c_depth`) and drives `main.py` as a subprocess over the kit's real dev bars (`dataset/bars/CBBTC.ndjson.gz`). It uses float instead of the engine's bigint truncation — differences are <1 bp and cannot flip a +$6.19 result, but the official server grade is the only authoritative number.

## Protocol (exact, from the official kit — not assumed)

Source: kit `protocol.schema.json`, `RULES.txt`, `sdk/makefun_bars.py`, and competition API `GET /api/competitions/btc-race-24h-20261117-0603` + `/requirements` (fetched 2026-11-17). Harness `agent-fun/bars-replay/v1`, NDJSON:

- → `{"type":"hello","protocol":"agent-fun/bars-replay/v1","seed":str,"symbols":[{"symbol":"CBBTC","mint":"cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij","price_scale":5,"qty_scale":8}],"capital":{"quote":"USDC","quote_scale":6,"quote_units":"1000000000"},"bar_interval":"1m","caps":{"max_open_positions":1,"max_orders_per_bar":16,"max_weight_bps":10000},"fees":{"fee_bps":4,"slippage_bps":2,"min_notional_quote_units":"1000000"},"latency_bars":1}` (optional `warmup_bars`)
- → per minute `{"type":"batch","seq":int,"t":ms,"bars":[{"symbol","t","o","h","l","c","v":null,"o_depth":[str],"c_depth":[str]} — prices integer strings at scale 5],"fills":[…],"account":{"cash_quote_units":str,"equity_quote_units":str,"positions":[{"symbol","qty":str,"cost_quote_units":str}]}}`
- ← one reply per batch, flushed: `{"type":"reply","seq":<echoed>,"orders":[{"side":"buy","client_id":"o1","symbol":"CBBTC","notional_quote_units":"<int str>","max_slippage_bps":150}]}` (sells carry `"qty"` instead of `"notional_quote_units"`)
- → `{"type":"done","seq":int,"account":{…}}` ends the run.
- Orders decided on bar *k* fill at bar *k+1*'s open (4 bps fee + 2 bps slippage + constant-product impact vs that bar's `o_depth`; a $500 trade vs the dev window's ~$790k depth pays ≈5–8 bps impact per side, ≈27 bps round trip all-in). Last bar's orders are rejected; leftovers are flattened at the last close. Long-only, ≤1 position, ≤16 orders/reply, min buy $1. `main.py` implements this wire format directly (byte-identical reply encoding to the SDK's `encode_reply`); it does not import the SDK shim, so there is no `sys.path`/vendor dependency under the grader's `python3 -P -s` invocation.

## Strategy

Mean-reversion dip buying, sized for the cost stack (a trade must expect >~27 bps to be worth taking):

- **Entry (flat only):** close ≥0.60% below its 90-bar SMA → buy 50% of equity. Freefall filter: skip if close < 97% of the 240-bar SMA, or the last 15 bars fell >2.5%, or the close undercut the prior 30-bar low by >1%. 30-bar cooldown after every exit.
- **Exit:** +1.0% over cost (fees included, from the harness account), or back at SMA·1.002 once gain ≥ +0.3% (costs covered — a falling SMA never forces a loss sale), or −1.8% stop, or 240-bar time cap.
- **Crash/risk brakes:** 15-bar fall worse than −3% while holding → sell immediately; equity 8% below its peak → no new entries for 240 bars.
- **Guaranteed-trade fallback:** if nothing has been sent by bar 1000 and the tape isn't crashing, one 25% probe buy (TP +0.6% / SL −1.5% / 120 bars) so the ≥1-trade guardrail is met even in a dead tape.
- Dev-window evidence: the window is sideways (+0.49% buy-and-hold, ±1.2% range, B&H max DD 2.1%); dips under the short mean preceded +0.56% average 60-minute moves (89% positive). A trend-following module (EMA/momentum) was built, tested and **removed** — it lost on dev (−$5.66 vs +$3.50 for MR-only at the time) and on 3 of 4 synthetic regimes. Parameter sweeps (SMA 45/60/90, dip 0.5–1.2%, size 50–70%, stops, cooldowns) are summarised by the final table above; SMA-90/size-50% was the most robust combination, not the dev-maximal one.

## Risks / honest caveats

- Dev margin is real but thin (+0.62%): the race window is unseen live prices; in a strong sustained uptrend this bot mostly sits out (only the fallback trades) and will finish near $0 — which still meets the guardrails, and places are paid to the top 10 *whatever their sign* among guardrail-meeting entries.
- Local PnL comes from the float re-implementation described above, not from `grade-local` (Docker unavailable here); re-grade with `node cli/makefun.mjs grade-local --kit . --bot <repo> --driver docker` before submitting if Docker is available.
- Submission itself (not done here) requires the user's own GitHub repo created after 2026-11-17 06:03 UTC, an OSI license (MIT `LICENSE` included in this folder), a Solana wallet signature, and the agent-session transcript bundle — see kit `RULES.txt` "Enter".

## Files

- `main.py` — the entry (self-contained, stdlib only).
- `test_harness.py` — local simulator + scenario/determinism tests (not part of the entry).
- `LICENSE` — MIT.
