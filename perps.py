# perps.py
"""PERPS: SOL, ETH and BTC on Jupiter Perps, swing-traded with the trend.

Two styles (PERP_STYLE):
- swing (the default): the 1-hour chart decides the direction, the 15-minute chart the entry, and each trade is out within
  a few hours: the whole position is closed at the last swing high (low for a short), at least 1.5R and at most 3R away.
- trend: the 4-hour chart decides the direction and the 1-hour chart the entry; half is closed at +2R, the rest trails.

The entry is the same in both: in an uptrend it waits for a dip into the entry chart's 20 EMA, the RSI to cool under 45
and turn back up, and a green candle closing back over the EMA; it then buys when the price breaks that candle's high.
Shorts are the mirror image. It never enters right after a big candle, never when the price is stretched more than one
ATR over its average, and never at the recent high (no buying the top).

Every trade has a stop under the dip it bought (over the bounce it shorted). The whole balance goes in each trade
(PERP_STAKE_PCT=100, what you chose), at the leverage the stop allows, up to PERP_MAX_LEVERAGE (50): the liquidation price
is always at least 1.5x as far as the stop, and a stop-out costs about half the balance at most (PERP_MAX_LOSS_PCT).
At +1R the stop moves to break-even. It stands aside after a violent candle and around news times you give it.

replay() runs the same rules over the last week of real candles, so the page can show what they would have done.

This first version trades a FAKE balance only (PERP_PAPER_BANK_USD, $50), with Jupiter's fees (0.06% to open and to
close, on the full size), an hourly borrow fee and Jupiter's liquidation rule. Real orders come next.

Prices: Kraken's public candles and ticker (no key), Coinbase's when Kraken doesn't answer.
"""
import bisect
import logging
import math
import threading
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
STYLES = ("swing", "trend")
TF_MIN = {"m15": 15, "h1": 60, "h4": 240}
TF_NAME = {"m15": "15-minute", "h1": "1-hour", "h4": "4-hour"}
TF_SHORT = {"m15": "15m", "h1": "1h", "h4": "4h"}


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
        style = str(env.get("PERP_STYLE", "swing")).strip().lower() or "swing"
        if style not in STYLES:
            raise ValueError("PERP_STYLE must be swing or trend")
        self.style = style
        swing = style == "swing"
        # swing: the 1-hour trend, entries on the 15-minute chart, out within hours. trend: the 4-hour trend, 1-hour entries
        self.entry_tf, self.trend_tf = ("m15", "h1") if swing else ("h1", "h4")
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
        # swing: the whole trade closes at the last swing high / low, but never under 1.5R or over 3R away (PERP_TP1_R,
        # PERP_TP_MAX_R). trend: half closes at +2R and the rest trails PERP_TRAIL_ATR behind the best price
        self.tp1_r = _env_num(env, "PERP_TP1_R", 1.5 if swing else 2, low=0.1, high=20)
        self.tp_max_r = _env_num(env, "PERP_TP_MAX_R", 3, low=0.1, high=50)
        if self.tp_max_r < self.tp1_r:
            raise ValueError("PERP_TP_MAX_R must be at least PERP_TP1_R")
        self.tp1_pct = _env_num(env, "PERP_TP1_PCT", 100 if swing else 50, high=100)
        self.trail_atr = _env_num(env, "PERP_TRAIL_ATR", 1.5, low=0.1, high=20)
        self.max_hold_hours = _env_num(env, "PERP_MAX_HOLD_HOURS", 3 if swing else 48, low=0.25, high=24 * 30)
        self.stretch_atr = _env_num(env, "PERP_STRETCH_ATR", 1, low=0.1, high=10)
        self.spike_atr = _env_num(env, "PERP_SPIKE_ATR", 2.5, low=0.5, high=20)
        self.shock_atr = _env_num(env, "PERP_SHOCK_ATR", 3.5, low=0.5, high=20)
        self.cooldown_minutes = _env_num(env, "PERP_COOLDOWN_MINUTES", 30 if swing else 120, high=24 * 60)
        self.min_trend = _env_num(env, "PERP_MIN_TREND", 0.3, high=20)              # trend chart 20/50 EMA gap, in its ATRs
        self.candle_seconds = _env_num(env, "PERP_CANDLE_SECONDS", 60 if swing else 120, low=30)
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
        return {k: getattr(self, k) for k in ("style", "entry_tf", "trend_tf", "tp_max_r", "coins", "paper_bank", "max_leverage", "stake_pct", "min_collateral",
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
    """Coinbase has 15-minute and 1-hour candles (no 4-hour): 4-hour ones are built from the 1-hour ones, on the same UTC
    boundaries as Kraken's."""
    body = _get(f"{COINBASE}/products/{COINS[coin]['coinbase']}/candles", {"granularity": 900 if minutes == 15 else 3600})
    if not isinstance(body, list):
        raise collect.FetchFailed("coinbase", "no candles in the answer")
    hours = [c for c in (_candle(r[0], r[3], r[2], r[1], r[4]) for r in body if isinstance(r, list) and len(r) >= 5) if c]
    hours.sort(key=lambda c: c["t"])
    if len(hours) < 2:
        raise collect.FetchFailed("coinbase", "too few candles")
    if minutes in (15, 60):
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
    """What the charts say for one coin. h1: the entry chart's CLOSED candles, oldest first (15-minute in the swing style,
    1-hour in the trend style); h4: the trend chart's (1-hour / 4-hour).
    Returns {"coin", "trend", "setup" (or None), "wait": [...], "atr", "atr_pct", ...}."""
    now = now if now is not None else time.time()
    E, T = TF_NAME[cfg.entry_tf], TF_NAME[cfg.trend_tf]
    step = TF_MIN[cfg.entry_tf] * 60
    look_h = round(24 * step / 3600)                             # the "recent high": the last 24 entry candles
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
    if now - b["t"] > 3 * step:
        out["wait"].append("the price data is stale")
        return out
    if trend == "flat":
        out["wait"].append(f"no clear {T} trend: it only trades with one")
        return out
    if (b["h"] - b["l"]) > cfg.shock_atr * a:
        out["wait"].append(f"a violent {E} candle ({(b['h'] - b['l']) / a:.1f}x the usual range): standing aside")
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
        out["wait"].append(f"waiting for a {word} to the {E} 20 EMA ({_px(e20[-1])})")
    elif too_deep:
        out["wait"].append(f"the {word} went past the {E} 50 EMA: the trend may be breaking")
    elif not cooled:
        out["wait"].append("the RSI hasn't cooled off yet" + (" (under 45)" if long else " (over 55)"))
    elif not turned:
        out["wait"].append(f"waiting for a {'green' if long else 'red'} {E} candle closing back "
                           f"{'over' if long else 'under'} the 20 EMA, the RSI turning")
    elif spike:
        out["wait"].append("the last candle was too big: no entry after a spike")
    elif stretched:
        out["wait"].append(f"stretched {abs(b['c'] - e20[-1]) / a:.1f} ATR from the 20 EMA: no buying the top" if long
                           else f"stretched {abs(b['c'] - e20[-1]) / a:.1f} ATR from the 20 EMA: no shorting the bottom")
    elif at_top:
        out["wait"].append(f"right at the {look_h}-hour high: no buying the top" if long
                           else f"right at the {look_h}-hour low: no shorting the bottom")
    if out["wait"]:
        return out
    trigger = b["h"] + 0.05 * a if long else b["l"] - 0.05 * a
    stop = dip - 0.15 * a if long else dip + 0.15 * a
    out["setup"] = {"side": "long" if long else "short", "trigger": trigger, "stop": stop, "bar": b["t"],
                    "expires": b["t"] + step + max(step, 1800),      # the trigger must break within 30 min (1 hour: trend)
                    "chase_limit": trigger + 0.3 * a if long else trigger - 0.3 * a,
                    "atr": a, "score": round(strength + (45 - min(r[-6:]) if long else max(r[-6:]) - 55) / 10, 2),
                    "why": [f"{T} trend {trend} (20 EMA {'over' if long else 'under'} the 50, strength {strength:.1f})",
                            f"{E} {word} to the 20 EMA, RSI {'cooled to' if long else 'heated to'} "
                            f"{min(r[-6:]) if long else max(r[-6:]):.0f} and turning",
                            f"{'green' if long else 'red'} candle closed {'over' if long else 'under'} the 20 EMA"]}
    if cfg.style == "swing":                     # the target: the last swing high (low), the top of the move it dipped from
        swing_px = max(c["h"] for c in h1[-24:]) if long else min(c["l"] for c in h1[-24:])
        out["setup"]["swing"] = swing_px
        out["setup"]["target"] = target_for(trigger, stop, swing_px, long, cfg)
    return out


def target_for(entry, stop, swing_px, long, cfg):
    """The swing style's take-profit: the last swing high (low), but at least PERP_TP1_R and at most PERP_TP_MAX_R away."""
    r = abs(entry - stop)
    sgn = 1 if long else -1
    lo, hi = entry + sgn * cfg.tp1_r * r, entry + sgn * cfg.tp_max_r * r
    want = swing_px if swing_px is not None else lo
    return min(max(want, lo), hi) if long else max(min(want, lo), hi)


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


class _MemState:
    """A State that lives in memory only: the replay's."""

    def __init__(self):
        self.lock, self.data = threading.RLock(), {}

    def save(self):
        pass


REPLAY_WINDOW = 130                              # candles of history each step reads (enough for the 50 EMA)


def replay(data, cfg, bank=None):
    """The rules, run over past candles. data: {coin: {"entry": [...], "trend": [...]}}, the CLOSED candles of the entry and
    trend charts, oldest first. It is the live bot itself (the same entries, stops, targets, fees, borrow, liquidation,
    cooldown: one trade at a time, the whole balance in each) fed the candles one at a time. Inside a candle the price is
    walked open -> one extreme -> the other -> close, in the order that's WORSE for the trade (down first when it's long;
    on the candle it enters: in at the trigger, then the drop), stopping at every trigger, stop, target and liquidation
    price on the way. So when one candle touched both the stop and the target, it counts the stop."""
    step = TF_MIN[cfg.entry_tf] * 60
    tstep = TF_MIN[cfg.trend_tf] * 60
    clock = [0.0]
    st = _MemState()
    p = Perps(cfg, st, clock=lambda: clock[0])
    p.refresh = lambda now: None
    with st.lock:
        p.d["mode"] = "auto"
        if bank is not None:
            p.d["bank"] = p.d["start_bank"] = float(bank)
    coins = [c for c in cfg.coins if c in data and len(data[c].get("entry") or []) > 60 and len(data[c].get("trend") or []) > 60]
    ent = {c: data[c]["entry"] for c in coins}
    trd = {c: data[c]["trend"] for c in coins}
    trd_t = {c: [x["t"] + tstep for x in trd[c]] for c in coins}          # when each trend candle closed
    at = {c: {x["t"]: i for i, x in enumerate(ent[c])} for c in coins}
    times = sorted({x["t"] for c in coins for x in ent[c]})
    warm = 60
    first = None
    for t in times:
        live = [c for c in coins if at[c].get(t, -1) >= warm]
        if not live:
            continue
        first = first if first is not None else t
        bars = {c: ent[c][at[c][t]] for c in live}
        clock[0] = t + 1
        for c, b in bars.items():
            p.prices[c] = b["o"]
        p.tick(clock[0])
        pos = p.d.get("position")
        order = sorted(live, key=lambda c: (0 if pos and pos["coin"] == c else 1, -((p.d.get("armed") or {}).get(c) or {}).get("score", -99)))
        for c in order:
            b = bars[c]
            pos = p.d.get("position")
            a = (p.d.get("armed") or {}).get(c)
            if pos and pos["coin"] == c:                      # an open trade: against it first
                path = [b["l"], b["h"], b["c"]] if pos["side"] == "long" else [b["h"], b["l"], b["c"]]
            elif a and a["side"] == "long" and b["o"] < a["trigger"] <= b["h"]:
                path = [a["trigger"], b["l"], b["h"], b["c"]]  # the worst order: in at the trigger, then the drop, then the rise
            elif a and a["side"] == "short" and b["o"] > a["trigger"] >= b["l"]:
                path = [a["trigger"], b["h"], b["l"], b["c"]]
            else:
                path = [b["l"], b["h"], b["c"]]
            x = b["o"]
            for k, goal in enumerate(path):
                ts = t + step * (k + 1) / (len(path) + 1)
                for _ in range(8):                             # every level on the way: trigger, stop, target, liquidation
                    lv = _levels(p, c)
                    between = [v for v in lv if (x < v < goal) or (goal < v < x)]
                    nxt = min(between, key=lambda v: abs(v - x)) if between else goal
                    p.prices[c] = nxt
                    clock[0] = ts
                    p.tick(ts)
                    x = nxt
                    if nxt == goal:
                        break
        clock[0] = t + step                                    # the candle closed: read the charts
        for c in live:
            i = at[c][t]
            j = bisect.bisect_right(trd_t[c], t + step)
            if j < 60:
                continue
            p.prices[c] = bars[c]["c"]
            p.reads[c] = read_coin(c, ent[c][max(0, i + 1 - REPLAY_WINDOW): i + 1], trd[c][max(0, j - REPLAY_WINDOW): j], cfg, t + step)
        p.tick(t + step)
    if p.d.get("position"):
        p.close(1.0, p.prices[p.d["position"]["coin"]], "the replay ended (still open)", clock[0])
    closed = [x for x in p.d.get("trades") or [] if x.get("final")]
    won = [x for x in closed if (x.get("total") or 0) > 0]
    end = float(p.d.get("bank") or 0.0)
    start = float(p.d.get("start_bank") or 0.0)
    peak, dd, eq = start, 0.0, start
    for x in reversed(p.d.get("trades") or []):
        eq += x.get("pnl") or 0.0
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 0.0)
    days = (times[-1] + step - first) / 86400 if first is not None else 0.0
    return {"style": cfg.style, "entry_tf": cfg.entry_tf, "trend_tf": cfg.trend_tf, "coins": coins,
            "from": _now_iso(first) if first is not None else None, "to": _now_iso(times[-1] + step) if times else None,
            "days": round(days, 1), "trades": len(closed), "won": len(won), "lost": len(closed) - len(won),
            "start": round(start, 2), "end": round(end, 2), "pnl_pct": round(100 * (end / start - 1), 1) if start else None,
            "max_drawdown_pct": round(100 * dd, 1), "liquidated": sum(1 for x in closed if str(x.get("why", "")).startswith("LIQ")),
            "avg_hold_min": round(sum(x.get("held_min") or 0 for x in closed) / len(closed), 1) if closed else None,
            "per_day": round(len(closed) / days, 2) if days else None,
            "stopped": end < cfg.min_collateral,
            "list": [{k: x.get(k) for k in ("time", "opened_at", "coin", "side", "leverage", "entry", "exit", "pnl", "pct", "why", "final",
                                             "held_min", "total")} for x in (p.d.get("trades") or [])[:40]]}


def _levels(p, coin):
    """The prices the replay must stop at for one coin: its trigger, or the open trade's stop, target, break-even point
    and liquidation."""
    out = []
    pos = p.d.get("position")
    if pos and pos["coin"] == coin:
        sgn = 1 if pos["side"] == "long" else -1
        out += [pos["stop"], pos.get("liq") or liq_price(pos, p.cfg)]
        if pos.get("target") and not pos.get("tp1"):
            out.append(pos["target"])
        elif not pos.get("tp1"):
            out.append(pos["entry"] + sgn * p.cfg.tp1_r * pos["r"])
        if not pos.get("be"):
            out.append(pos["entry"] + sgn * p.cfg.be_r * pos["r"])
    a = (p.d.get("armed") or {}).get(coin)
    if a and not pos:
        out += [a["trigger"], a.get("chase_limit")]
    return [v for v in out if v and math.isfinite(v)]


class Perps:
    """The perps desk. state: the runner's State (one JSON file); everything perps keeps is under state.data["perps"]."""

    def __init__(self, cfg, state, clock=time.time):
        self.cfg, self.state, self.clock = cfg, state, clock
        self.candles = {}                      # coin -> {"m15"/"h1"/"h4": [closed...], "cur_m15"...: forming, "at_m15"...: ts, "src"}
        self.prices, self.price_at, self.price_src = {}, 0.0, None
        self.reads = {}
        self.data_error = None
        self.tried = {}                            # coin -> when its candles were last asked for
        self.price_fail_at = 0.0
        self.down = {}                             # source -> until when it's left alone after failing
        self.replay_result, self.replay_busy, self.replay_key = None, False, None
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
    def _every(self, tf):
        """How often a timeframe's candles are fetched: the two the bot trades from every PERP_CANDLE_SECONDS, the other
        one (only drawn on the chart) every 15 minutes."""
        return self.cfg.candle_seconds if tf in (self.cfg.entry_tf, self.cfg.trend_tf) else max(900.0, self.cfg.candle_seconds)

    def maybe_replay(self, now, wait=False):
        """Each new entry-chart candle, the rules are replayed over the candles it has (about a week of 15-minute ones), in
        the background: the page shows what they would have done."""
        if self.replay_busy or not all(self.has_data(c) for c in self.cfg.coins):
            return
        key = tuple(self.candles[c][self.cfg.entry_tf][-1]["t"] for c in self.cfg.coins)
        if key == self.replay_key:
            return
        self.replay_key, self.replay_busy = key, True
        snap = {c: {"entry": list(self.candles[c][self.cfg.entry_tf]), "trend": list(self.candles[c][self.cfg.trend_tf])}
                for c in self.cfg.coins}

        def job():
            t0 = time.time()
            try:
                out = replay(snap, self.cfg)
                out.update(took_s=round(time.time() - t0, 2), at=_now_iso(self.clock()))
                self.replay_result = out
            except Exception as exc:              # a replay problem never touches the trading
                self.replay_result = {"error": f"{type(exc).__name__}: {exc}"[:200], "at": _now_iso(self.clock())}
            finally:
                self.replay_busy = False
        if wait:
            job()
        else:
            threading.Thread(target=job, daemon=True, name="perps-replay").start()

    def has_data(self, coin):
        k = self.candles.get(coin) or {}
        return bool(k.get(self.cfg.entry_tf)) and bool(k.get(self.cfg.trend_tf))

    def refresh(self, now):
        """Candles every PERP_CANDLE_SECONDS, the price every few seconds. A source that fails is left alone for a minute
        (the other one stands in), so a dead price feed never slows the loop down."""
        sources = [(n, fn) for n, fn in (("Kraken", kraken_candles), ("Coinbase", coinbase_candles)) if now >= self.down.get(n, 0)]
        for c in self.cfg.coins:
            k = self.candles.setdefault(c, {})
            due = [tf for tf in (self.cfg.entry_tf, self.cfg.trend_tf, *[t for t in TF_MIN if t not in (self.cfg.entry_tf, self.cfg.trend_tf)])
                   if now - k.get("at_" + tf, 0) >= self._every(tf)]
            if not due or now - self.tried.get(c, 0) < min(60.0, self.cfg.candle_seconds):
                continue
            self.tried[c] = now
            for tf in due:
                got, why = None, []
                for name, fn in sources:
                    if now < self.down.get(name, 0):
                        why.append(f"{name} down")
                        continue
                    try:
                        closed, cur = fn(c, TF_MIN[tf])
                        got = name.lower()
                        break
                    except collect.FetchFailed as exc:
                        self.down[name] = now + 60
                        why.append(f"{name} {exc}")
                if not got:
                    self.data_error = f"{c} {TF_NAME[tf]} candles: " + ("; ".join(why) or "both sources down")[:180]
                    break
                k[tf], k["cur_" + tf], k["at_" + tf], k["src"] = closed, cur, now, got
            else:
                k["at"] = now
                self.data_error = None
            break                                  # one coin per step: Kraken is asked a couple of times, never in a burst
        self.maybe_replay(now)
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
            if self.has_data(c):
                k = self.candles[c]
                self.reads[c] = read_coin(c, k[self.cfg.entry_tf], k[self.cfg.trend_tf], self.cfg, now)
        if self.mode() == "off":
            return did
        self.arm(now)
        if not self.d.get("position") and self.mode() == "auto":
            did = self.enter(now) or did
        return did

    def arm(self, now):
        """A setup on a closed candle waits for its trigger (the price breaking that candle's high / low) for the next
        30 minutes (an hour in the trend style). A setup that was already traded isn't armed again."""
        with self.state.lock:
            armed = self.d.setdefault("armed", {})
            taken = self.d.setdefault("taken", {})
            for c, rd in self.reads.items():
                s = rd.get("setup")
                old = armed.get(c)
                if s and (not old or old.get("bar") != s["bar"]) and taken.get(c) != s["bar"]:
                    armed[c] = {**s, "coin": c}
                    self.event("signal", f"{c} {s['side'].upper()} setup: {'buys' if s['side'] == 'long' else 'shorts'} a break of "
                               f"{_px(s['trigger'])}, stop {_px(s['stop'])}" + (f", target {_px(s['target'])}" if s.get("target") else "")
                               + ". " + "; ".join(s["why"]), c)
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
        target = target_for(entry, stop, a.get("swing"), side == "long", cfg) if cfg.style == "swing" else None
        size = stake * lev
        fee = size * cfg.fee_frac("open")
        pos = {"coin": coin, "side": side, "entry": entry, "size": size, "size0": size, "stake": stake, "stake_left": stake,
               "collateral": stake - fee, "leverage": round(lev, 1), "stop": stop, "stop0": stop, "r": abs(entry - stop),
               "best": entry, "opened_ts": now, "opened_at": _now_iso(now), "borrow": 0.0, "borrow_ts": now,
               "open_fee": round(fee, 4), "open_fee_left": fee, "fees": fee, "tp1": False, "be": False, "realized": 0.0, "mode": "paper",
               "why": a.get("why") or [], "atr": a.get("atr"), "target": target, "style": cfg.style}
        pos["liq"] = liq_price(pos, cfg)
        with self.state.lock:
            self.d["bank"] = round(bank - stake, 2)
            self.d["position"] = pos
            self.d["armed"] = {}
            self.d.setdefault("taken", {})[coin] = a.get("bar")          # this setup is used: never traded twice
            self.state.save()
        self.event("open", f"OPENED {coin} {side.upper()} {lev:.1f}x: ${stake:,.2f} in, ${size:,.0f} position at {_px(entry)}; "
                   f"stop {_px(stop)} ({stop_pct:.2f}% away, a stop-out costs about ${self._loss_at(pos, stop):,.2f}); "
                   + (f"target {_px(target)} (+${-self._loss_at(pos, target):,.2f}); " if target else "")
                   + f"liquidation {_px(pos['liq'])}", coin)
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
            fees_px = pos["entry"] * (cfg.fee_frac("open") + cfg.fee_frac("close") + cfg.slippage_bps / 1e4)   # truly even
            be = pos["entry"] + fees_px if long else pos["entry"] - fees_px
            with self.state.lock:
                pos["stop"] = max(pos["stop"], be) if long else min(pos["stop"], be)
                pos["be"] = True
            self.event("info", f"{pos['coin']}: up {gain_r:.1f}R, the stop moves to break-even ({_px(pos['stop'])})", pos["coin"])
        tgt = pos.get("target")
        at_target = (price >= tgt if long else price <= tgt) if tgt else gain_r >= cfg.tp1_r
        if not pos["tp1"] and at_target and cfg.tp1_pct > 0:
            with self.state.lock:
                pos["tp1"] = True
            self.close(cfg.tp1_pct / 100, price, (f"target hit at {_px(tgt)} (+{gain_r:.1f}R)" if tgt else f"take profit at +{gain_r:.1f}R"), now)
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
            return self.close(1.0, price, f"the {TF_NAME[cfg.trend_tf]} trend turned {trend}", now)
        if now - pos["opened_ts"] >= cfg.max_hold_hours * 3600:
            return self.close(1.0, price, f"held {cfg.max_hold_hours:g}h, the limit: out", now)
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
                "settings": self.cfg.settings(), "real": "not yet: real Jupiter Perps orders come in the next version",
                "replay": self.replay_result, "replay_running": self.replay_busy}

    def chart(self, coin, tf="1h"):
        """One coin's chart for the page: candles (the last one live, at the latest price), its 20 and 50 EMAs, the
        open trade's levels (entry, stop, break-even, take-profit, liquidation), a setup's trigger, and the entries and
        exits of its trades. GET /api/perps-chart?coin=SOL&tf=1h."""
        coin = str(coin or "").strip().upper()
        if coin not in self.cfg.coins:
            return 404, {"detail": "unknown coin"}
        tf = {"15m": "15m", "4h": "4h"}.get(str(tf or "").strip().lower(), "1h")
        key = {"15m": "m15", "1h": "h1", "4h": "h4"}[tf]
        k = self.candles.get(coin) or {}
        price = self.prices.get(coin)
        out = {"coin": coin, "tf": tf, "price": price, "candles": [], "ema20": [], "ema50": [], "levels": [], "markers": [],
               "updated": _now_iso(self.clock()), "source": k.get("src"), "style": self.cfg.style}
        if not k.get(key):
            out["note"] = f"no {TF_NAME[key]} candles for {coin} yet: " + (self.data_error or "the first read takes a minute")
            return 200, out
        series = list(k[key])
        cur = k.get("cur_" + key)
        if cur and (not series or cur["t"] > series[-1]["t"]):
            cur = dict(cur)
            if price:
                cur.update(c=price, h=max(cur["h"], price), l=min(cur["l"], price))
            series.append(cur)
        closes = [c["c"] for c in series]
        e20, e50 = ema(closes, 20), ema(closes, 50)
        keep = 90 if tf == "4h" else 96
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
            if pos.get("target") and not pos.get("tp1"):
                lv.append(("tp", "TARGET", pos["target"]))
            elif not pos.get("tp1"):
                lv.append(("tp", f"TAKE {self.cfg.tp1_pct:g}% PROFIT", pos["entry"] + sgn * self.cfg.tp1_r * pos["r"]))
            out["levels"] = [{"kind": a, "label": b, "price": r6(v)} for a, b, v in lv]
            out["position"] = {"side": pos["side"], "leverage": pos["leverage"], "opened": pos["opened_ts"]}
        a = (self.d.get("armed") or {}).get(coin)
        if a and not (pos and pos["coin"] == coin):
            out["levels"] += [{"kind": "trigger", "label": "BUYS ABOVE" if a["side"] == "long" else "SHORTS BELOW", "price": r6(a["trigger"])},
                              {"kind": "stop", "label": "ITS STOP", "price": r6(a["stop"])}]
            if a.get("target"):
                out["levels"].append({"kind": "tp", "label": "ITS TARGET", "price": r6(a["target"])})
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
