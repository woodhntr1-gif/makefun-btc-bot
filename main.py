#!/usr/bin/env python3
"""cbBTC minute-bar trading bot for make.fun harness agent-fun/bars-replay/v1.

Protocol (exact, from the competition kit: protocol.schema.json, RULES.txt and
sdk/makefun_bars.py of btc-race-24h-20261117-0603, fetched 2026-11-17):
  stdin  (NDJSON): {"type":"hello","protocol":"agent-fun/bars-replay/v1","seed":str,
                    "symbols":[{"symbol":"CBBTC","mint":...,"price_scale":5,"qty_scale":8}],
                    "capital":{"quote":"USDC","quote_scale":6,"quote_units":"1000000000"},
                    "bar_interval":"1m","first_close_ms":int,
                    "caps":{"max_open_positions":1,"max_orders_per_bar":16,
                            "max_weight_bps":10000,"shorts":false,"leverage":false},
                    "fees":{"fee_bps":4,"slippage_bps":2,"min_notional_quote_units":"1000000"},
                    "latency_bars":1, optional "warmup_bars":int}
                   {"type":"batch","seq":int,"t":ms,"bars":[{symbol,t,o,h,l,c,v,
                    o_depth:[str],c_depth:[str]} prices as integer strings at price_scale],
                    "fills":[...], "account":{"cash_quote_units":str,
                    "equity_quote_units":str,"positions":[{symbol,qty,cost_quote_units}]}}
                   {"type":"done","seq":int,"account":{...}}
  stdout (one reply per batch, flushed):
                   {"type":"reply","seq":<batch seq>,"orders":[
                     {"side":"buy","client_id":"o1","symbol":"CBBTC",
                      "notional_quote_units":"<int str>","max_slippage_bps":150},
                     {"side":"sell","client_id":"o2","symbol":"CBBTC",
                      "qty":"<int str>","max_slippage_bps":150}]}
  Orders decided on bar k fill at bar k+1's open, 4 bps fee + 2 bps slippage +
  constant-product impact vs that bar's o_depth. Long only, max 1 position.

Strategy (deterministic, float64 + - * / only, no clock/network/files/env):
  Hybrid, built for a sideways-to-trending BTC tape and a hard dev gate
  (dev net PnL must be > $0 after fees) plus race guardrails (>=1 trade,
  max drawdown <= 25%):
  1. Mean-reversion dip buying: when flat and the close is at least 0.60%
     below its 90-bar SMA, buy 50% of equity -- unless the tape is in
     freefall (close < 97% of the 240-bar SMA, or the last 15 bars fell more
     than 2.5%, or the close undercut the 30-bar low by >1%). Exit at +1.0%
     over cost, at the 90-bar SMA +0.2% once the gain covers costs (>=+0.3%),
     at -1.8% under cost, or after 240 bars; a 30-bar cooldown follows every
     exit. On the frozen dev window, dips under the short mean preceded a
     +0.56% average 60-minute move, 89% positive -- several times the ~0.27%
     round-trip cost (fees + slippage + impact at this size). A trend-
     following module was tested and removed: it lost on the dev window and
     on 3 of 4 synthetic regimes (see README).
  2. Risk brakes: a 15-bar drop worse than -3% while holding sells at once;
     if equity falls 8% under its peak, no new entries for 240 bars.
  3. Guaranteed-trade fallback: if no order has been sent by bar 1000 and
     the tape is not crashing, one 25%-of-equity probe buy (TP +0.6%,
     SL -1.5%, 120-bar cap) so the race guardrail of >=1 trade is met.
"""

import json
import sys

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 150

# --- strategy constants -------------------------------------------------
SMA_FAST_N = 90          # mean-reversion anchor
SMA_SLOW_N = 240         # regime / freefall filter
EMA_FAST_N = 20
EMA_SLOW_N = 120
DIP = 0.0060             # buy when close <= SMA60 * (1 - DIP)
FREEFALL_SLOW = 0.97     # no dip buys below 97% of SMA240
FREEFALL_15 = -0.025     # no dip buys after a >2.5% 15-bar fall
NEW_LOW = 0.99           # no dip buys >1% under the 30-bar low
MR_SIZE = 0.50           # fraction of equity per dip buy
MR_TP_GAIN = 0.010
MR_TP_SMA = 1.002        # exit at SMA60 * 1.002
STOP_GAIN = -0.018
MR_MAX_HOLD = 240
TREND_MOM60 = 0.008
TREND_SIZE = 0.50
TREND_TRAIL = 0.025
TREND_MAX_HOLD = 480
CRASH_15 = -0.03         # immediate exit on a >3% 15-bar fall while holding
DD_BRAKE = 0.08          # equity drawdown that pauses entries
BRAKE_BARS = 240
FALLBACK_BAR = 1000
FALLBACK_SIZE = 0.25
FALLBACK_TP = 0.006
FALLBACK_SL = -0.015
FALLBACK_MAX_HOLD = 120
COOLDOWN_BARS = 30       # no new dip buy for this long after any exit


class Bot:
    def __init__(self, hello):
        self.hello = hello
        sym = hello["symbols"][0]
        self.symbol = sym.get("symbol", SYMBOL)
        self.price_scale = 10 ** int(sym.get("price_scale", 5))
        self.qty_scale = 10 ** int(sym.get("qty_scale", 8))
        self.quote_scale = int(hello.get("capital", {}).get("quote_scale", 6))
        self.caps = hello.get("caps", {})
        self.fees = hello.get("fees", {})
        self.min_notional = int(self.fees.get("min_notional_quote_units", "1000000"))
        self.warmup = int(hello.get("warmup_bars", 0))
        self.closes = []            # float closes, one per bar of our symbol
        self.sma_fast_sum = 0.0
        self.sma_slow_sum = 0.0
        self.ema_fast = None
        self.ema_slow = None
        self.cash = int(hello["capital"]["quote_units"])
        self.equity = self.cash
        self.peak_equity = self.equity
        self.pos_qty = 0            # int, at qty_scale
        self.pos_cost = 0           # int quote units, fees included
        self.entry_bar = -1
        self.entry_kind = ""
        self.peak_close = 0.0
        self.bars_seen = 0
        self.traded = False         # any order sent (fills reported next batch)
        self.brake_until = -1
        self.cooldown_until = -1
        self.next_id = 1

    # -- account / indicators -------------------------------------------
    def apply_account(self, account):
        if not isinstance(account, dict):
            return
        self.cash = int(account.get("cash_quote_units", self.cash))
        self.equity = int(account.get("equity_quote_units", self.equity))
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        positions = account.get("positions") or []
        held = None
        for p in positions:
            if p.get("symbol") == self.symbol:
                held = p
        if held is not None and int(held.get("qty", "0")) > 0:
            if self.pos_qty == 0:
                self.entry_bar = self.bars_seen - 1
                if not self.entry_kind:
                    self.entry_kind = "mr"
                self.peak_close = self.closes[-1] if self.closes else 0.0
            self.pos_qty = int(held["qty"])
            self.pos_cost = int(held.get("cost_quote_units", "0"))
        else:
            self.pos_qty = 0
            self.pos_cost = 0
            self.entry_kind = ""
            self.peak_close = 0.0

    def push_close(self, close):
        self.closes.append(close)
        self.sma_fast_sum += close
        if len(self.closes) > SMA_FAST_N:
            self.sma_fast_sum -= self.closes[-SMA_FAST_N - 1]
        self.sma_slow_sum += close
        if len(self.closes) > SMA_SLOW_N:
            self.sma_slow_sum -= self.closes[-SMA_SLOW_N - 1]
        for attr, n in (("ema_fast", EMA_FAST_N), ("ema_slow", EMA_SLOW_N)):
            cur = getattr(self, attr)
            if cur is None:
                if len(self.closes) == n:
                    setattr(self, attr, sum(self.closes[-n:]) / n)
            else:
                k = 2.0 / (n + 1)
                setattr(self, attr, cur + k * (close - cur))

    def sma(self, n, total):
        if len(self.closes) < n:
            return None
        return total / n

    # -- orders -----------------------------------------------------------
    def _cid(self):
        cid = "o%d" % self.next_id
        self.next_id += 1
        return cid

    def buy(self, notional):
        notional = int(notional)
        if notional < self.min_notional or notional > self.cash:
            notional = min(notional, self.cash)
        if notional < self.min_notional:
            return None
        self.traded = True
        return {"side": "buy", "client_id": self._cid(), "symbol": self.symbol,
                "notional_quote_units": str(notional),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def sell_all(self):
        if self.pos_qty <= 0:
            return None
        self.traded = True
        self.cooldown_until = self.bars_seen + COOLDOWN_BARS
        return {"side": "sell", "client_id": self._cid(), "symbol": self.symbol,
                "qty": str(self.pos_qty), "max_slippage_bps": MAX_SLIPPAGE_BPS}

    # -- decision ---------------------------------------------------------
    def decide(self, seq):
        if not self.closes:
            return []
        close = self.closes[-1]
        n = len(self.closes)
        if close > self.peak_close and self.pos_qty > 0:
            self.peak_close = close
        ret15 = close / self.closes[-16] - 1.0 if n > 15 else 0.0

        if self.pos_qty > 0:
            # Position value at this close in quote units (float; only ever
            # compared against cost, never sent on the wire).
            value = (self.pos_qty * close * (10 ** self.quote_scale)
                     / (self.qty_scale * self.price_scale))
            gain = (value - self.pos_cost) / self.pos_cost if self.pos_cost > 0 else 0.0
            held_bars = self.bars_seen - 1 - self.entry_bar
            sma_fast = self.sma(SMA_FAST_N, self.sma_fast_sum)
            exit_now = False
            if ret15 <= CRASH_15:
                exit_now = True
            elif gain <= STOP_GAIN:
                exit_now = True
            elif self.entry_kind == "fallback":
                if gain >= FALLBACK_TP or gain <= FALLBACK_SL or held_bars >= FALLBACK_MAX_HOLD:
                    exit_now = True
            else:  # mean reversion
                if gain >= MR_TP_GAIN:
                    exit_now = True
                elif (sma_fast is not None and close >= sma_fast * MR_TP_SMA
                        and gain >= 0.003):
                    # back at the mean with costs covered: bank it; never let
                    # a falling SMA talk us into selling at a loss
                    exit_now = True
                elif held_bars >= MR_MAX_HOLD:
                    exit_now = True
            if exit_now:
                order = self.sell_all()
                return [order] if order else []
            return []

        # flat: risk brake
        if self.peak_equity > 0 and self.equity < self.peak_equity * (1.0 - DD_BRAKE):
            if self.brake_until < self.bars_seen:
                self.brake_until = self.bars_seen + BRAKE_BARS
        if self.bars_seen <= self.brake_until:
            return []
        if seq < self.warmup:
            return []

        sma_fast = self.sma(SMA_FAST_N, self.sma_fast_sum)
        sma_slow = self.sma(SMA_SLOW_N, self.sma_slow_sum)
        if sma_fast is None:
            return []

        # 1) mean-reversion dip buy
        if close <= sma_fast * (1.0 - DIP) and self.bars_seen > self.cooldown_until:
            freefall = False
            if sma_slow is not None and close < sma_slow * FREEFALL_SLOW:
                freefall = True
            if ret15 <= FREEFALL_15:
                freefall = True
            if n >= 31 and close < min(self.closes[-31:-1]) * NEW_LOW:
                freefall = True
            if not freefall:
                self.entry_kind = "mr"
                order = self.buy(self.equity * MR_SIZE)
                return [order] if order else []
            return []

        # 2) guaranteed-trade fallback (a trend-following module was tested
        #    and removed: it lost money on the dev window and on 3 of 4
        #    synthetic regimes; see README)
        if not self.traded and self.bars_seen >= FALLBACK_BAR and ret15 > -0.02:
            self.entry_kind = "fallback"
            order = self.buy(self.equity * FALLBACK_SIZE)
            return [order] if order else []
        return []

    def _trail_hit(self, close):
        # peak_close is tracked in raw price units like close
        return self.peak_close > 0 and close <= self.peak_close * (1.0 - TREND_TRAIL)


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
            continue
        if bot is None:
            continue
        if kind == "done":
            bot.apply_account(msg.get("account") or {})
            return
        if kind != "batch":
            continue
        bot.apply_account(msg.get("account") or {})
        for bar in msg.get("bars") or []:
            if bar.get("symbol") == bot.symbol:
                try:
                    bot.push_close(int(bar["c"]))
                except (KeyError, ValueError, TypeError):
                    pass
        bot.bars_seen += 1
        orders = bot.decide(int(msg.get("seq", 0)))
        max_orders = int(bot.caps.get("max_orders_per_bar", 16))
        orders = orders[:max_orders]
        reply = {"type": "reply", "seq": msg.get("seq"), "orders": orders}
        out.write(json.dumps(reply, separators=(",", ":"), ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
