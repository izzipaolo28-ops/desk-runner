# perps.py
"""PERPS: SOL, ETH and BTC on Jupiter Perps, traded on the trend.

The 4-hour chart decides the direction. The 1-hour chart decides the entry: in an uptrend it waits for a dip into the
1-hour 20 EMA, the RSI to cool under 45 and turn back up, and a green candle closing back over the EMA; it then buys when
the price breaks that candle's high. Shorts are the mirror image. It never enters right after a big candle, never when
the price is stretched more than one ATR over its average, and never at the 24-hour high (no buying the top).

Every trade has a stop under the dip it bought (over the bounce it shorted). The whole balance goes in each trade
(PERP_STAKE_PCT=100, what you chose), at the leverage the stop allows, up to PERP_MAX_LEVERAGE (50): the liquidation price
is always at least 1.5x as far as the stop, and a stop-out costs about half the balance at most (PERP_MAX_LOSS_PCT).
At +1R the stop moves to break-even, at +2R half is closed, and the rest
trails 1.5 ATR behind the best price. It stands aside after a violent hour and around news times you give it.

This first version trades a FAKE balance only (PERP_PAPER_BANK_USD, $50), with Jupiter's fees (0.06% to open and to
close, on the full size), an hourly borrow fee and Jupiter's liquidation rule. Real orders come next.

Prices: Kraken's public candles and ticker (no key), Coinbase's when Kraken doesn't answer.
"""
import logging
import math
import time
from datetime import datetime, timezone

import collect

log = logging.getLogger("perps")

KRAKEN = "https://api.kraken.com/0/public"
COINBASE = "https://api.exchange.coinbase.com"
COINS = {
    "SOL": {"kraken": "SOLUSD", "keys": ("SOLUSD",), "coinbase": "SOL-USD"},
    "ETH": {"kraken": "ETHUSD", "keys": ("XETHZUSD", "ETHUSD"), "coinbase": "ETH-USD"},
    "BTC": {"kraken": "XBTUSD", "keys": ("XXBTZUSD", "XBTUSD"), "coinbase": "BTC-USD"},
}
MODES = ("off", "watch", "auto")


def _num(v):
    return collect._num(v)


def _get(url, params=None):
    """One GET, no retries and no sleeping (the loop is shared with the memecoin bots; the other source is the retry)."""
    try:
        r = collect.HTTP.get(url, params=params, timeout=6)
    except Exception as exc:                       # requests' network errors, a closed connection...
        raise collect.FetchFailed(url, f"network error ({type(exc).__name__})") from None
    if r.status_code != 200:
        raise collect.FetchFailed(url, f"HTTP {r.status_code}", r.status_code)
    try:
        return r.json()
    except ValueError:
        raise collect.FetchFailed(url, "not JSON") from None


def _now_iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(timespec="seconds")


def _env_num(env, name, default, low=0.0, high=None):
    raw = str(env.get(name, "")).strip()
    if not raw:
        return float(default)
    try:
        v = float(raw)
    except ValueError:
        v = math.nan
    if not math.isfinite(v) or v < low or (high is not None and v > high):
        span = f"between {low:g} and {high:g}" if high is not None else f"at least {low:g}"
        raise ValueError(f"{name} must be a number {span}")
    return v


class PerpConfig:
    def __init__(self, env):
        mode = str(env.get("PERP_MODE", "auto")).strip().lower() or "auto"
        if mode not in MODES:
            raise ValueError("PERP_MODE must be off, watch or auto")
        self.mode = mode
        coins = [c.strip().upper() for c in str(env.get("PERP_COINS", "SOL,ETH,BTC")).split(",") if c.strip()]
        bad = [c for c in coins if c not in COINS]
        if bad or not coins:
            raise ValueError("PERP_COINS must be a list of SOL, ETH and BTC")
        self.coins = list(dict.fromkeys(coins))
        self.paper_bank = _env_num(env, "PERP_PAPER_BANK_USD", 50, low=10)
        self.max_leverage = _env_num(env, "PERP_MAX_LEVERAGE", 50, low=1.1, high=250)
        self.stake_pct = _env_num(env, "PERP_STAKE_PCT", 100, low=1, high=100)       # of the balance, every trade
        self.min_collateral = _env_num(env, "PERP_MIN_COLLATERAL_USD", 10, low=0)     # Jupiter's minimum
        # Jupiter's costs (docs.jup.ag perps FAQ): 0.06% of the size to open and to close, a price-impact fee on big
        # trades (an estimate here), and an hourly borrow fee on the full size (an estimate: it moves with the pool)
        self.open_fee_bps = _env_num(env, "PERP_OPEN_FEE_BPS", 6, high=100)
        self.close_fee_bps = _env_num(env, "PERP_CLOSE_FEE_BPS", 6, high=100)
        self.impact_bps = _env_num(env, "PERP_IMPACT_BPS", 1, high=100)
        self.borrow_pct_hour = _env_num(env, "PERP_BORROW_PCT_HOUR", 0.003, high=1)
        self.maint_pct = _env_num(env, "PERP_MAINT_PCT", 0.4, high=10)               # liquidated at 0.4% of size left (250x)
        self.slippage_bps = _env_num(env, "PERP_SLIPPAGE_BPS", 5, high=200)
        self.liq_buffer = _env_num(env, "PERP_LIQ_BUFFER", 1.5, low=1, high=10)      # liquidation >= 1.5x the stop away
        # what one stop-out may cost, as a share of the money in the trade: "about half" (what you picked). At 50x a stop
        # 1% away would cost 50%+fees; a wider stop gets less leverage so it still costs about half, never more
        self.max_loss_pct = _env_num(env, "PERP_MAX_LOSS_PCT", 50, low=1, high=100)
        self.min_stop_pct = _env_num(env, "PERP_MIN_STOP_PCT", 0.35, low=0.05, high=10)
        self.max_stop_pct = _env_num(env, "PERP_MAX_STOP_PCT", 3, low=0.1, high=20)
        self.be_r = _env_num(env, "PERP_BREAKEVEN_R", 1, high=10)
        self.tp1_r = _env_num(env, "PERP_TP1_R", 2, high=20)
        self.tp1_pct = _env_num(env, "PERP_TP1_PCT", 50, high=100)
        self.trail_atr = _env_num(env, "PERP_TRAIL_ATR", 1.5, low=0.1, high=20)
        self.max_hold_hours = _env_num(env, "PERP_MAX_HOLD_HOURS", 48, low=1, high=24 * 30)
        self.stretch_atr = _env_num(env, "PERP_STRETCH_ATR", 1, low=0.1, high=10)
        self.spike_atr = _env_num(env, "PERP_SPIKE_ATR", 2.5, low=0.5, high=20)
        self.shock_atr = _env_num(env, "PERP_SHOCK_ATR", 3.5, low=0.5, high=20)
        self.cooldown_minutes = _env_num(env, "PERP_COOLDOWN_MINUTES", 120, high=24 * 60)
        self.min_trend = _env_num(env, "PERP_MIN_TREND", 0.3, high=20)              # 4h 20/50 EMA gap, in 4h ATRs
        self.candle_seconds = _env_num(env, "PERP_CANDLE_SECONDS", 120, low=30)
        self.news = []
        for part in str(env.get("PERP_NEWS_TIMES", "")).split(","):
            part = part.strip()
            if part:
                try:
                    self.news.append(datetime.fromisoformat(part.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    raise ValueError("PERP_NEWS_TIMES must be UTC times like 2026-10-29T18:00Z, comma separated") from None
        self.news_minutes = _env_num(env, "PERP_NEWS_MINUTES", 60, high=24 * 60)

    def fee_frac(self, which):
        return ((self.open_fee_bps if which == "open" else self.close_fee_bps) + self.impact_bps) / 1e4

    def settings(self):
        return {k: getattr(self, k) for k in ("coins", "paper_bank", "max_leverage", "stake_pct", "min_collateral",
                                               "open_fee_bps", "close_fee_bps", "impact_bps", "borrow_pct_hour", "maint_pct",
                                               "liq_buffer", "max_loss_pct", "min_stop_pct", "max_stop_pct", "be_r", "tp1_r", "tp1_pct",
                                               "trail_atr", "max_hold_hours", "stretch_atr", "spike_atr", "shock_atr",
                                               "cooldown_minutes", "min_trend")}


# ---------- prices ----------
def _kraken_rows(body):
    if not isinstance(body, dict) or body.get("error"):
        raise collect.FetchFailed("kraken", "; ".join(map(str, (body or {}).get("error") or ["no answer"]))[:80])
    res = body.get("result") or {}
    for k, v in res.items():
        if k != "last" and isinstance(v, list):
            return v
    raise collect.FetchFailed("kraken", "no candles in the answer")


def _candle(t, o, h, l, c):
    vals = [_num(x) for x in (t, o, h, l, c)]
    if any(v is None for v in vals) or vals[2] < vals[3] or vals[4] <= 0:
        return None
    return {"t": int(vals[0]), "o": vals[1], "h": vals[2], "l": vals[3], "c": vals[4]}


def kraken_candles(coin, minutes):
    """(closed candles oldest first, the candle still forming). Kraken's last entry is the one still forming."""
    rows = _kraken_rows(_get(f"{KRAKEN}/OHLC", {"pair": COINS[coin]["kraken"], "interval": minutes}))
    out = [c for c in (_candle(r[0], r[1], r[2], r[3], r[4]) for r in rows if isinstance(r, list) and len(r) >= 5) if c]
    out.sort(key=lambda c: c["t"])
    if len(out) < 2:
        raise collect.FetchFailed("kraken", "too few candles")
    return out[:-1], out[-1]


def coinbase_candles(coin, minutes):
    """Coinbase has 1-hour candles (no 4-hour): 4-hour ones are built from them, on the same UTC boundaries as Kraken's."""
    body = _get(f"{COINBASE}/products/{COINS[coin]['coinbase']}/candles", {"granularity": 3600})
    if not isinstance(body, list):
        raise collect.FetchFailed("coinbase", "no candles in the answer")
    hours = [c for c in (_candle(r[0], r[3], r[2], r[1], r[4]) for r in body if isinstance(r, list) and len(r) >= 5) if c]
    hours.sort(key=lambda c: c["t"])
    if len(hours) < 2:
        raise collect.FetchFailed("coinbase", "too few candles")
    if minutes == 60:
        return hours[:-1], hours[-1]
    span = minutes * 60
    buckets = {}
    for c in hours:
        buckets.setdefault(c["t"] - c["t"] % span, []).append(c)
    out = []
    for t in sorted(buckets):
        cs = buckets[t]
        out.append({"t": t, "o": cs[0]["o"], "h": max(x["h"] for x in cs), "l": min(x["l"] for x in cs), "c": cs[-1]["c"],
                    "n": len(cs)})
    closed = [c for c in out[:-1] if c["n"] == span // 3600]
    return closed, out[-1]


def kraken_prices(coins):
    body = _get(f"{KRAKEN}/Ticker", {"pair": ",".join(COINS[c]["kraken"] for c in coins)})
    if not isinstance(body, dict) or body.get("error"):
        raise collect.FetchFailed("kraken", "ticker: " + "; ".join(map(str, (body or {}).get("error") or ["no answer"]))[:80])
    res, out = body.get("result") or {}, {}
    for c in coins:
        row = next((res[k] for k in COINS[c]["keys"] if k in res), None)
        p = _num(((row or {}).get("c") or [None])[0])
        if p and p > 0:
            out[c] = p
    return out


def coinbase_price(coin):
    body = _get(f"{COINBASE}/products/{COINS[coin]['coinbase']}/ticker")
    p = _num((body or {}).get("price")) if isinstance(body, dict) else None
    if not p or p <= 0:
        raise collect.FetchFailed("coinbase", "no price")
    return p


# ---------- indicators ----------
def ema(values, n):
    out, k, e = [], 2 / (n + 1), None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def atr(candles, n=14):
    out, a, prev = [], None, None
    for c in candles:
        tr = c["h"] - c["l"] if prev is None else max(c["h"] - c["l"], abs(c["h"] - prev), abs(c["l"] - prev))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
        prev = c["c"]
    return out


def rsi(closes, n=14):
    out, up, dn = [], None, None
    for i, c in enumerate(closes):
        if i == 0:
            out.append(50.0)
            continue
        d = c - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        up = g if up is None else (up * (n - 1) + g) / n
        dn = l if dn is None else (dn * (n - 1) + l) / n
        out.append(100.0 if dn == 0 and up > 0 else 50.0 if dn == 0 else 100 - 100 / (1 + up / dn))
    return out


# ---------- the read: trend, setup, why it waits ----------
def read_coin(coin, h1, h4, cfg, now=None):
    """What the charts say for one coin. h1 / h4: CLOSED candles, oldest first.
    Returns {"coin", "trend", "setup" (or None), "wait": [...], "atr", "atr_pct", ...}."""
    now = now if now is not None else time.time()
    out = {"coin": coin, "trend": "unknown", "setup": None, "wait": []}
    if len(h4) < 60 or len(h1) < 60:
        out["wait"].append("not enough price history yet")
        return out
    c4 = [c["c"] for c in h4]
    e20_4, e50_4 = ema(c4, 20), ema(c4, 50)
    a4 = atr(h4)[-1]
    strength = abs(e20_4[-1] - e50_4[-1]) / a4 if a4 else 0.0     # how far apart the 20 and 50 EMAs are, in 4h ATRs
    if strength < cfg.min_trend:
        trend = "flat"
    elif c4[-1] > e50_4[-1] and e20_4[-1] > e50_4[-1] and e50_4[-1] > e50_4[-7]:
        trend = "up"
    elif c4[-1] < e50_4[-1] and e20_4[-1] < e50_4[-1] and e50_4[-1] < e50_4[-7]:
        trend = "down"
    else:
        trend = "flat"
    closes = [c["c"] for c in h1]
    e20, e50, a1, r = ema(closes, 20), ema(closes, 50), atr(h1), rsi(closes)
    b, a = h1[-1], a1[-1]
    out.update(trend=trend, strength=round(strength, 2), price=b["c"], ema20=e20[-1], ema50=e50[-1], atr=a,
               atr_pct=round(100 * a / b["c"], 3), rsi=round(r[-1], 1), bar_time=b["t"])
    if now - b["t"] > 3 * 3600:
        out["wait"].append("the price data is stale")
        return out
    if trend == "flat":
        out["wait"].append("no clear 4-hour trend: it only trades with one")
        return out
    if (b["h"] - b["l"]) > cfg.shock_atr * a:
        out["wait"].append(f"a violent hour ({(b['h'] - b['l']) / a:.1f}x the usual range): standing aside")
        return out
    near = [t for t in cfg.news if abs(now - t) <= cfg.news_minutes * 60]
    if near:
        out["wait"].append("news time: standing aside " + datetime.fromtimestamp(near[0], timezone.utc).strftime("%b %d %H:%M UTC"))
        return out
    last6 = h1[-6:]
    long = trend == "up"
    if long:
        dip = min(c["l"] for c in last6)
        pulled = dip <= e20[-1] + 0.25 * a
        too_deep = dip < e50[-1] - 1.0 * a
        cooled = min(r[-6:]) < 45
        turned = b["c"] > b["o"] and b["c"] > e20[-1] and r[-1] > r[-2]
        stretched = b["c"] - e20[-1] > cfg.stretch_atr * a
        at_top = b["c"] >= max(c["h"] for c in h1[-24:]) - 0.25 * a
    else:
        dip = max(c["h"] for c in last6)
        pulled = dip >= e20[-1] - 0.25 * a
        too_deep = dip > e50[-1] + 1.0 * a
        cooled = max(r[-6:]) > 55
        turned = b["c"] < b["o"] and b["c"] < e20[-1] and r[-1] < r[-2]
        stretched = e20[-1] - b["c"] > cfg.stretch_atr * a
        at_top = b["c"] <= min(c["l"] for c in h1[-24:]) + 0.25 * a
    spike = (b["h"] - b["l"]) > cfg.spike_atr * a
    word = "dip" if long else "bounce"
    if not pulled:
        out["wait"].append(f"waiting for a {word} to the 1-hour 20 EMA ({_px(e20[-1])})")
    elif too_deep:
        out["wait"].append(f"the {word} went past the 1-hour 50 EMA: the trend may be breaking")
    elif not cooled:
        out["wait"].append("the RSI hasn't cooled off yet" + (" (under 45)" if long else " (over 55)"))
    elif not turned:
        out["wait"].append(f"waiting for a {'green' if long else 'red'} 1-hour candle closing back "
                           f"{'over' if long else 'under'} the 20 EMA, the RSI turning")
    elif spike:
        out["wait"].append("the last candle was too big: no entry after a spike")
    elif stretched:
        out["wait"].append(f"stretched {abs(b['c'] - e20[-1]) / a:.1f} ATR from the 20 EMA: no buying the top" if long
                           else f"stretched {abs(b['c'] - e20[-1]) / a:.1f} ATR from the 20 EMA: no shorting the bottom")
    elif at_top:
        out["wait"].append("right at the 24-hour high: no buying the top" if long else "right at the 24-hour low: no shorting the bottom")
    if out["wait"]:
        return out
    trigger = b["h"] + 0.05 * a if long else b["l"] - 0.05 * a
    stop = dip - 0.15 * a if long else dip + 0.15 * a
    out["setup"] = {"side": "long" if long else "short", "trigger": trigger, "stop": stop, "bar": b["t"],
                    "expires": b["t"] + 2 * 3600, "chase_limit": trigger + 0.3 * a if long else trigger - 0.3 * a,
                    "atr": a, "score": round(strength + (45 - min(r[-6:]) if long else max(r[-6:]) - 55) / 10, 2),
                    "why": [f"4-hour trend {trend} (20 EMA {'over' if long else 'under'} the 50, strength {strength:.1f})",
                            f"1-hour {word} to the 20 EMA, RSI {'cooled to' if long else 'heated to'} "
                            f"{min(r[-6:]) if long else max(r[-6:]):.0f} and turning",
                            f"{'green' if long else 'red'} candle closed {'over' if long else 'under'} the 20 EMA"]}
    return out


def _px(v):
    v = _num(v)
    if v is None:
        return "?"
    return f"${v:,.2f}" if v >= 100 else f"${v:,.3f}" if v >= 1 else f"${v:.5f}"


# ---------- the trade math (Jupiter's) ----------
def choose_leverage(stop_pct, cfg):
    """The most leverage that (1) keeps the liquidation price at least PERP_LIQ_BUFFER x the stop away, and (2) keeps a
    stop-out from costing more than PERP_MAX_LOSS_PCT of the money in the trade, up to PERP_MAX_LEVERAGE.
    Liquidation distance ~ 1/lev - maint - fees; a stop-out costs ~ lev x (stop + fees) of the stake."""
    s = stop_pct / 100
    fees = cfg.fee_frac("open") + cfg.fee_frac("close")
    need = cfg.liq_buffer * s + cfg.maint_pct / 100 + fees
    by_liq = 1 / need if need > 0 else cfg.max_leverage
    by_loss = (cfg.max_loss_pct / 100) / (s + fees) if s + fees > 0 else cfg.max_leverage
    return max(1.1, min(cfg.max_leverage, by_liq, by_loss))


def liq_price(pos, cfg):
    size, entry = pos["size"], pos["entry"]
    room = pos["collateral"] - pos.get("borrow", 0.0) - size * cfg.maint_pct / 100 - size * cfg.fee_frac("close")
    move = room / size
    return entry * (1 - move) if pos["side"] == "long" else entry * (1 + move)


def pnl_at(pos, price):
    d = 1 if pos["side"] == "long" else -1
    return pos["size"] * (price / pos["entry"] - 1) * d


class Perps:
    """The perps desk. state: the runner's State (one JSON file); everything perps keeps is under state.data["perps"]."""

    def __init__(self, cfg, state, clock=time.time):
        self.cfg, self.state, self.clock = cfg, state, clock
        self.candles = {}                      # coin -> {"h1": [...], "h4": [...], "at": ts, "src": "kraken"}
        self.prices, self.price_at, self.price_src = {}, 0.0, None
        self.reads = {}
        self.data_error = None
        self.tried = {}                            # coin -> when its candles were last asked for
        self.price_fail_at = 0.0
        with self.state.lock:
            p = self.state.data.setdefault("perps", {})
            p.setdefault("bank", cfg.paper_bank)
            p.setdefault("start_bank", cfg.paper_bank)
            p.setdefault("position", None)
            p.setdefault("trades", [])
            p.setdefault("events", [])
            p.setdefault("armed", {})

    # ----- state helpers -----
    @property
    def d(self):
        return self.state.data["perps"]

    def mode(self):
        m = self.d.get("mode")
        return m if m in MODES else self.cfg.mode

    def set_mode(self, mode):
        mode = str(mode or "").strip().lower()
        if mode not in MODES:
            return 400, "mode must be off, watch or auto"
        with self.state.lock:
            self.d["mode"] = mode
            self.state.save()
        self.event("info", {"off": "PERPS OFF: no new trades (an open trade is still managed to its exit)",
                            "watch": "PERPS on WATCH: it reads the charts and says what it would do, no trades",
                            "auto": "PERPS on AUTO: it trades the setups (fake money)"}[mode])
        return 200, f"perps set to {mode.upper()}"

    def event(self, kind, text, coin=None):
        with self.state.lock:
            ev = self.d.setdefault("events", [])
            ev.insert(0, {"time": _now_iso(self.clock()), "kind": kind, "coin": coin, "text": str(text)[:220]})
            del ev[60:]

    # ----- data -----
    def refresh(self, now):
        """Candles every PERP_CANDLE_SECONDS, the price every few seconds. When both sources fail it waits a minute before
        trying again, so a dead price feed never slows the memecoin bots (they share the loop)."""
        due = [c for c in self.cfg.coins if now - (self.candles.get(c) or {}).get("at", 0) >= self.cfg.candle_seconds
               and now - self.tried.get(c, 0) >= 60]
        dead = set()                               # a source that just failed isn't asked again for the next coin
        for c in due:
            self.tried[c] = now
            got, why = None, []
            for name, fn in (("Kraken", kraken_candles), ("Coinbase", coinbase_candles)):
                if name in dead:
                    why.append(f"{name} down")
                    continue
                try:
                    h1, cur1 = fn(c, 60)
                    h4, cur4 = fn(c, 240)
                    got = name.lower()
                    break
                except collect.FetchFailed as exc:
                    dead.add(name)
                    why.append(f"{name} {exc}")
            if not got:
                self.data_error = f"{c} candles: " + "; ".join(why)[:180]
                continue
            src = got
            self.candles[c] = {"h1": h1, "h4": h4, "cur1": cur1, "cur4": cur4, "at": now, "src": src}
            self.data_error = None
        pos = self.d.get("position")
        armed = self.d.get("armed") or {}
        fast = bool(pos) or bool(armed)
        if now - self.price_at >= (4 if fast else 30) and now - self.price_fail_at >= (10 if pos else 60):
            self.price_at = now
            try:
                self.prices.update(kraken_prices(self.cfg.coins))
                self.price_src = "kraken"
            except collect.FetchFailed:
                failed = False
                for c in self.cfg.coins:
                    try:
                        self.prices[c] = coinbase_price(c)
                        self.price_src = "coinbase"
                    except collect.FetchFailed as exc:
                        self.data_error, failed = f"{c} price: {exc}"[:200], True
                        break                      # Coinbase is down too: try again later, not once per coin
                if failed:
                    self.price_fail_at = now

    # ----- the loop -----
    def tick(self, now=None):
        now = now if now is not None else self.clock()
        if self.mode() == "off" and not self.d.get("position"):
            return "perps: off"
        self.refresh(now)
        did = "perps: watching"
        if self.d.get("position"):
            did = self.manage(now) or "perps: holding"
        for c in self.cfg.coins:
            k = self.candles.get(c)
            if k:
                self.reads[c] = read_coin(c, k["h1"], k["h4"], self.cfg, now)
        if self.mode() == "off":
            return did
        self.arm(now)
        if not self.d.get("position") and self.mode() == "auto":
            did = self.enter(now) or did
        return did

    def arm(self, now):
        """A setup on a closed candle waits for its trigger (the price breaking that candle's high / low) for the next hour."""
        with self.state.lock:
            armed = self.d.setdefault("armed", {})
            for c, rd in self.reads.items():
                s = rd.get("setup")
                old = armed.get(c)
                if s and (not old or old.get("bar") != s["bar"]):
                    armed[c] = {**s, "coin": c}
                    self.event("signal", f"{c} {s['side'].upper()} setup: buys a break of {_px(s['trigger'])}, stop {_px(s['stop'])}. "
                               + "; ".join(s["why"]), c)
            for c in list(armed):
                a = armed[c]
                tr = (self.reads.get(c) or {}).get("trend")
                if now > a["expires"] or (tr and tr != ("up" if a["side"] == "long" else "down")):
                    armed.pop(c)

    def enter(self, now):
        armed = self.d.get("armed") or {}
        if not armed:
            return None
        last_loss = self.d.get("last_loss_ts") or 0
        if now - last_loss < self.cfg.cooldown_minutes * 60:
            return None
        ready = []
        for c, a in armed.items():
            p = self.prices.get(c)
            if not p:
                continue
            hit = p >= a["trigger"] if a["side"] == "long" else p <= a["trigger"]
            chased = p > a["chase_limit"] if a["side"] == "long" else p < a["chase_limit"]
            if hit and not chased:
                ready.append((a["score"], c, a, p))
            elif hit and chased:
                a["note"] = "ran past the trigger too fast: not chasing it"
        if not ready:
            return None
        ready.sort(key=lambda x: -x[0])
        _, coin, a, price = ready[0]
        return self.open(coin, a, price, now)

    def open(self, coin, a, price, now):
        cfg, side = self.cfg, a["side"]
        bank = float(self.d.get("bank") or 0.0)
        stake = round(bank * cfg.stake_pct / 100, 2)
        if stake < cfg.min_collateral:
            self.event("skip", f"{coin}: ${stake:,.2f} is under Jupiter's ${cfg.min_collateral:g} minimum: no trade", coin)
            with self.state.lock:
                self.d["armed"].pop(coin, None)
            return "perps: under the minimum"
        slip = cfg.slippage_bps / 1e4
        entry = price * (1 + slip) if side == "long" else price * (1 - slip)
        stop = a["stop"]
        stop_pct = abs(entry - stop) / entry * 100
        if (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
            with self.state.lock:
                self.d["armed"].pop(coin, None)
            return None
        if stop_pct < cfg.min_stop_pct:
            stop_pct = cfg.min_stop_pct
            stop = entry * (1 - stop_pct / 100) if side == "long" else entry * (1 + stop_pct / 100)
        if stop_pct > cfg.max_stop_pct:
            self.event("skip", f"{coin}: the stop would be {stop_pct:.2f}% away (max {cfg.max_stop_pct:g}%): too far for leverage", coin)
            with self.state.lock:
                self.d["armed"].pop(coin, None)
            return "perps: stop too far"
        lev = choose_leverage(stop_pct, cfg)
        size = stake * lev
        fee = size * cfg.fee_frac("open")
        pos = {"coin": coin, "side": side, "entry": entry, "size": size, "size0": size, "stake": stake, "stake_left": stake,
               "collateral": stake - fee, "leverage": round(lev, 1), "stop": stop, "stop0": stop, "r": abs(entry - stop),
               "best": entry, "opened_ts": now, "opened_at": _now_iso(now), "borrow": 0.0, "borrow_ts": now,
               "open_fee": round(fee, 4), "open_fee_left": fee, "fees": fee, "tp1": False, "be": False, "realized": 0.0, "mode": "paper",
               "why": a.get("why") or [], "atr": a.get("atr")}
        pos["liq"] = liq_price(pos, cfg)
        with self.state.lock:
            self.d["bank"] = round(bank - stake, 2)
            self.d["position"] = pos
            self.d["armed"] = {}
            self.state.save()
        self.event("open", f"OPENED {coin} {side.upper()} {lev:.1f}x: ${stake:,.2f} in, ${size:,.0f} position at {_px(entry)}; "
                   f"stop {_px(stop)} ({stop_pct:.2f}% away, a stop-out costs about ${self._loss_at(pos, stop):,.2f}); "
                   f"liquidation {_px(pos['liq'])}", coin)
        return "perps: opened"

    def _loss_at(self, pos, price):
        return -(pnl_at(pos, price) - pos["size"] * self.cfg.fee_frac("close") - pos.get("borrow", 0.0) - (pos["stake_left"] - pos["collateral"]))

    def manage(self, now):
        cfg = self.cfg
        pos = self.d["position"]
        price = self.prices.get(pos["coin"])
        if not price:
            return "perps: no price for the open trade"
        long = pos["side"] == "long"
        with self.state.lock:                     # the hourly borrow fee, on the full size
            hrs = max(0.0, now - pos.get("borrow_ts", now)) / 3600
            pos["borrow"] = pos.get("borrow", 0.0) + pos["size"] * cfg.borrow_pct_hour / 100 * hrs
            pos["borrow_ts"] = now
            pos["best"] = max(pos["best"], price) if long else min(pos["best"], price)
            pos["mark"] = price
            pos["liq"] = liq_price(pos, cfg)
        equity = pos["collateral"] + pnl_at(pos, price) - pos["borrow"]
        if equity - pos["size"] * cfg.fee_frac("close") <= pos["size"] * cfg.maint_pct / 100 or \
                (long and price <= pos["liq"]) or (not long and price >= pos["liq"]):
            return self.close(1.0, pos["liq"], "LIQUIDATED: the price reached the liquidation price", now, liquidated=True)
        hit = price <= pos["stop"] if long else price >= pos["stop"]
        if hit:
            slip = cfg.slippage_bps / 1e4
            fill = min(price, pos["stop"]) * (1 - slip) if long else max(price, pos["stop"]) * (1 + slip)
            why = ("trailing stop" if pos["tp1"] else "break-even stop" if pos["be"] else "stop-loss")
            return self.close(1.0, fill, f"{why} at {_px(pos['stop'])}", now)
        gain_r = ((price - pos["entry"]) if long else (pos["entry"] - price)) / pos["r"] if pos["r"] else 0.0
        if not pos["be"] and gain_r >= cfg.be_r:
            fees_px = pos["entry"] * (cfg.fee_frac("open") + cfg.fee_frac("close"))
            be = pos["entry"] + fees_px if long else pos["entry"] - fees_px
            with self.state.lock:
                pos["stop"] = max(pos["stop"], be) if long else min(pos["stop"], be)
                pos["be"] = True
            self.event("info", f"{pos['coin']}: up {gain_r:.1f}R, the stop moves to break-even ({_px(pos['stop'])})", pos["coin"])
        if not pos["tp1"] and gain_r >= cfg.tp1_r and cfg.tp1_pct > 0:
            with self.state.lock:
                pos["tp1"] = True
            self.close(cfg.tp1_pct / 100, price, f"take profit at +{gain_r:.1f}R", now)
            pos = self.d.get("position")
            if not pos:
                return "perps: closed"
        if pos["tp1"]:                            # the rest trails 1.5 ATR behind the best price
            a = (self.reads.get(pos["coin"]) or {}).get("atr") or pos.get("atr")
            if a:
                trail = pos["best"] - cfg.trail_atr * a if long else pos["best"] + cfg.trail_atr * a
                with self.state.lock:
                    pos["stop"] = max(pos["stop"], trail) if long else min(pos["stop"], trail)
        trend = (self.reads.get(pos["coin"]) or {}).get("trend")
        if trend in ("up", "down") and trend != ("up" if long else "down"):
            return self.close(1.0, price, f"the 4-hour trend turned {trend}", now)
        if now - pos["opened_ts"] >= cfg.max_hold_hours * 3600:
            return self.close(1.0, price, f"held {cfg.max_hold_hours:g}h, the limit", now)
        return "perps: holding"

    def close(self, frac, price, why, now, liquidated=False):
        cfg = self.cfg
        with self.state.lock:
            pos = self.d.get("position")
            if not pos:
                return None
            frac = 1.0 if frac >= 0.999 else frac
            part = pos["size"] * frac
            coll = pos["collateral"] * frac
            borrow = pos["borrow"] * frac
            stake = pos["stake_left"] * frac
            fee = part * cfg.fee_frac("close")
            of = pos.get("open_fee_left", 0.0) * frac
            pos["open_fee_left"] = pos.get("open_fee_left", 0.0) - of
            pnl = part * (price / pos["entry"] - 1) * (1 if pos["side"] == "long" else -1)
            back = 0.0 if liquidated else max(0.0, coll + pnl - fee - borrow)
            result = back - stake
            self.d["bank"] = round(float(self.d.get("bank") or 0.0) + back, 2)
            pos["size"] -= part
            pos["collateral"] -= coll
            pos["borrow"] -= borrow
            pos["stake_left"] -= stake
            pos["realized"] = pos.get("realized", 0.0) + result
            pos["fees"] = pos.get("fees", 0.0) + fee
            final = frac >= 1.0 or pos["size"] <= 1e-9
            row = {"time": _now_iso(now), "coin": pos["coin"], "side": pos["side"], "leverage": pos["leverage"],
                   "entry": pos["entry"], "exit": price, "stake": round(stake, 2), "size": round(part, 2),
                   "pnl": round(result, 2), "pct": round(100 * result / stake, 1) if stake else None,
                   "fees": round(fee + of, 4), "borrow": round(borrow, 4),
                   "why": why, "final": final, "mode": "paper", "opened_at": pos["opened_at"],
                   "held_min": round((now - pos["opened_ts"]) / 60, 1), "total": round(pos["realized"], 2) if final else None}
            self.d["trades"] = ([row] + list(self.d.get("trades") or []))[:200]
            if final:
                self.d["position"] = None
                if pos["realized"] < 0:
                    self.d["last_loss_ts"] = now
            self.state.save()
        verb = "CLOSED" if final else f"SOLD {round(frac * 100)}% of"
        self.event("close" if final else "partial", f"{verb} {pos['coin']} {pos['side'].upper()} at {_px(price)}: {why}, "
                   f"${result:+,.2f}" + (f" (this trade ${pos['realized']:+,.2f})" if final else ""), pos["coin"])
        return "perps: closed" if final else "perps: took profit"

    def request_close(self):
        pos = self.d.get("position")
        if not pos:
            return 409, "no perps trade open"
        price = self.prices.get(pos["coin"]) or pos.get("mark")
        if not price:
            return 409, "no price right now: try again in a few seconds"
        self.close(1.0, price, "closed by you", self.clock())
        return 200, f"closed {pos['coin']} {pos['side']}"

    # ----- what the page shows -----
    def status(self):
        with self.state.lock:
            d = {k: v for k, v in self.d.items() if k not in ("trades", "events")}
            trades = list(self.d.get("trades") or [])[:30]
            events = list(self.d.get("events") or [])[:40]
        pos = d.get("position")
        equity = float(d.get("bank") or 0.0)
        if pos:
            mark = self.prices.get(pos["coin"]) or pos.get("mark") or pos["entry"]
            pos = dict(pos)
            pos["mark"] = mark
            pos["pnl"] = round(pnl_at(pos, mark) - pos.get("borrow", 0.0) - (pos["stake_left"] - pos["collateral"])
                               - pos["size"] * self.cfg.fee_frac("close"), 2)
            pos["pnl_pct"] = round(100 * pos["pnl"] / pos["stake_left"], 1) if pos["stake_left"] else None
            pos["value"] = round(pos["stake_left"] + pos["pnl"], 2)
            pos["stop_loss_usd"] = round(self._loss_at(pos, pos["stop"]), 2)
            equity += max(0.0, pos["value"])
        closed = [t for t in (self.state.data["perps"].get("trades") or []) if t.get("final")]
        wins = sum(1 for t in closed if (t.get("total") or 0) > 0)
        reads = {c: dict(r) for c, r in self.reads.items()}
        return {"mode": self.mode(), "money": "fake", "bank": d.get("bank"), "start_bank": d.get("start_bank"),
                "equity": round(equity, 2), "position": pos, "armed": d.get("armed") or {}, "reads": reads,
                "prices": dict(self.prices), "price_source": self.price_src, "data_error": self.data_error,
                "trades": trades, "events": events, "record": {"closed": len(closed), "won": wins, "lost": len(closed) - wins},
                "cooldown_until": _now_iso(d["last_loss_ts"] + self.cfg.cooldown_minutes * 60)
                if d.get("last_loss_ts") and self.clock() - d["last_loss_ts"] < self.cfg.cooldown_minutes * 60 else None,
                "settings": self.cfg.settings(), "real": "not yet: real Jupiter Perps orders come in the next version"}

    def chart(self, coin, tf="1h"):
        """One coin's chart for the page: candles (the last one live, at the latest price), its 20 and 50 EMAs, the
        open trade's levels (entry, stop, break-even, take-profit, liquidation), a setup's trigger, and the entries and
        exits of its trades. GET /api/perps-chart?coin=SOL&tf=1h."""
        coin = str(coin or "").strip().upper()
        if coin not in self.cfg.coins:
            return 404, {"detail": "unknown coin"}
        tf = "4h" if str(tf or "").strip().lower() == "4h" else "1h"
        k = self.candles.get(coin)
        price = self.prices.get(coin)
        out = {"coin": coin, "tf": tf, "price": price, "candles": [], "ema20": [], "ema50": [], "levels": [], "markers": [],
               "updated": _now_iso(self.clock()), "source": (k or {}).get("src")}
        if not k:
            out["note"] = self.data_error or "no prices yet: the first read takes a minute"
            return 200, out
        series = list(k["h1" if tf == "1h" else "h4"])
        cur = k.get("cur1" if tf == "1h" else "cur4")
        if cur and (not series or cur["t"] > series[-1]["t"]):
            cur = dict(cur)
            if price:
                cur.update(c=price, h=max(cur["h"], price), l=min(cur["l"], price))
            series.append(cur)
        closes = [c["c"] for c in series]
        e20, e50 = ema(closes, 20), ema(closes, 50)
        keep = 96 if tf == "1h" else 90
        r6 = lambda v: float(f"{v:.6g}")
        out["candles"] = [[c["t"], r6(c["o"]), r6(c["h"]), r6(c["l"]), r6(c["c"])] for c in series[-keep:]]
        out["ema20"], out["ema50"] = [r6(v) for v in e20[-keep:]], [r6(v) for v in e50[-keep:]]
        t0 = out["candles"][0][0] if out["candles"] else 0
        pos = self.d.get("position")
        if pos and pos["coin"] == coin:
            sgn = 1 if pos["side"] == "long" else -1
            lv = [("entry", "ENTRY", pos["entry"]), ("stop", "TRAILING STOP" if pos.get("tp1") else "STOP AT BREAK-EVEN"
                  if pos.get("be") else "STOP", pos["stop"]), ("liq", "LIQUIDATION", liq_price(pos, self.cfg))]
            if not pos.get("be"):
                lv.append(("be", "STOP TO BREAK-EVEN AT", pos["entry"] + sgn * self.cfg.be_r * pos["r"]))
            if not pos.get("tp1"):
                lv.append(("tp", f"TAKE {self.cfg.tp1_pct:g}% PROFIT", pos["entry"] + sgn * self.cfg.tp1_r * pos["r"]))
            out["levels"] = [{"kind": a, "label": b, "price": r6(v)} for a, b, v in lv]
            out["position"] = {"side": pos["side"], "leverage": pos["leverage"], "opened": pos["opened_ts"]}
        a = (self.d.get("armed") or {}).get(coin)
        if a and not (pos and pos["coin"] == coin):
            out["levels"] += [{"kind": "trigger", "label": "BUYS ABOVE" if a["side"] == "long" else "SHORTS BELOW", "price": r6(a["trigger"])},
                              {"kind": "stop", "label": "ITS STOP", "price": r6(a["stop"])}]
        seen = set()
        for t in self.d.get("trades") or []:
            if t.get("coin") != coin:
                continue
            try:
                te = datetime.fromisoformat(str(t.get("time")).replace("Z", "+00:00")).timestamp()
                to = datetime.fromisoformat(str(t.get("opened_at")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if te >= t0:
                out["markers"].append({"t": te, "price": r6(t["exit"]), "kind": "close" if t.get("final") else "partial",
                                       "side": t["side"], "pnl": t.get("pnl"), "why": str(t.get("why") or "")[:80]})
            if to >= t0 and to not in seen:
                seen.add(to)
                out["markers"].append({"t": to, "price": r6(t["entry"]), "kind": "open", "side": t["side"]})
        if pos and pos["coin"] == coin and pos["opened_ts"] >= t0 and pos["opened_ts"] not in seen:
            out["markers"].append({"t": pos["opened_ts"], "price": r6(pos["entry"]), "kind": "open", "side": pos["side"]})
        return 200, out

    def reset_bank(self):
        if self.d.get("position"):
            return 409, "close the open perps trade first"
        with self.state.lock:
            self.d["bank"] = self.cfg.paper_bank
            self.d["start_bank"] = self.cfg.paper_bank
            self.d["last_loss_ts"] = None
            self.state.save()
        self.event("info", f"fake perps balance reset to ${self.cfg.paper_bank:,.2f}")
        return 200, "reset"
