# seats.py
"""SIZE, FILLS and RISK, in code. Arithmetic stays in code; nobody re-reads `why`.

Paper mode (LIVE_MODE=false, the default): the whole loop runs on real Jupiter quotes and real prices, but
nothing is signed or sent. Live mode signs with the bot's own wallet and lets Jupiter land the swap.
Up to MAX_POSITIONS coins at once (default 5), each with its own plan; RISK alone sells. The book is released
when the last one is sold.
Never an invented number: a seat that lacks a figure it needs refuses and says which one.
"""
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone

import book
import collect
import thresholds
from jupiter import USDC, WSOL, JupiterError
from main import TICKET_SHARE
from solana_wallet import sign_transaction

log = logging.getLogger("seats")
FEED_MAX = 150
MAX_POOL_SHARE = 0.02          # SIZE step 3: never more than 2% of the pool, or you are the exit
LAMPORTS = 1_000_000_000


def _env_num(env, name, default, low=0.0, high=None):
    """A number from the variables, or the default when the variable is unset. A variable that is set but is not a
    number in range is refused, never quietly replaced: a typo in STOP_LOSS_PCT must not turn the stop off.
    The message names the variable, never its value."""
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


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def mc(v):
    """$458.2K / $1.58M: market caps the way people read them. Unknown stays unknown."""
    v = _f(v)
    if v is None:
        return "MC unknown"
    return f"${v / 1e9:.2f}B" if v >= 1e9 else f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}K" if v >= 1e3 else f"${v:.0f}"


def supply_from(mcap, price):
    """Market cap / price = the supply the market cap is counted on (DexScreener's marketCap, never FDV)."""
    mcap, price = _f(mcap), _f(price)
    return mcap / price if mcap and price and mcap > 0 and price > 0 else None


class Config:
    def __init__(self, env=os.environ):
        live = str(env.get("LIVE_MODE", "false")).strip().lower()
        if live not in ("true", "false"):
            raise ValueError("LIVE_MODE must be exactly true or false")
        self.live = live == "true"
        self.paper_bank_usd = _env_num(env, "PAPER_BANK_USD", 1000, low=1)
        self.min_ticket_usd = _env_num(env, "MIN_TICKET_USD", 5)
        self.max_impact_pct = _env_num(env, "MAX_IMPACT_PCT", 3, high=50)
        self.fee_reserve_sol = _env_num(env, "FEE_RESERVE_SOL", 0.02)
        self.max_daily_loss_pct = _env_num(env, "MAX_DAILY_LOSS_PCT", 20, high=100)
        self.stop_loss_pct = _env_num(env, "STOP_LOSS_PCT", 30, high=100)   # sell everything at -30%; 0 = off
        self.risk_poll_seconds = _env_num(env, "RISK_POLL_SECONDS", 300, low=30)
        self.data_dir = env.get("DATA_DIR", "data")
        # WATCH: the price manager that runs while a position is open (all percentages, all 0 = that rule off)
        watch = str(env.get("WATCH", "on")).strip().lower()
        if watch not in ("on", "off"):
            raise ValueError("WATCH must be on or off")
        self.watch = watch == "on"
        self.watch_seconds = _env_num(env, "WATCH_SECONDS", 60, low=15)
        self.take_profit_pct = _env_num(env, "TAKE_PROFIT_PCT", 0)           # sell everything at +X%
        self.scale_out = parse_ladder(env.get("SCALE_OUT", "30:25,60:25,100:25"))
        self.trail_arm_pct = _env_num(env, "TRAIL_ARM_PCT", 20)              # trailing stop arms once up this much
        self.trail_pct = _env_num(env, "TRAIL_PCT", 15, high=100)            # ...then sells all this far off the peak
        self.drop_m5_pct = _env_num(env, "WATCH_DROP_M5_PCT", 8, high=100)   # "turning down": 5-min drop, sellers ahead
        self.derisk_pct = _env_num(env, "WATCH_DERISK_PCT", 50, high=100)    # ...sell this share of what's left
        self.pump_m5_pct = _env_num(env, "WATCH_PUMP_M5_PCT", 20)            # "spike": 5-min jump while in profit
        self.pump_sell_pct = _env_num(env, "WATCH_PUMP_SELL_PCT", 30, high=100)
        self.max_hold_hours = _env_num(env, "MAX_HOLD_HOURS", 24)
        self.max_positions = int(_env_num(env, "MAX_POSITIONS", 5, low=1, high=10))
        self.max_exposure_pct = _env_num(env, "MAX_EXPOSURE_PCT", 25, low=1, high=100)   # all coins together

    def watch_plan(self):
        return {"enabled": self.watch, "seconds": self.watch_seconds, "stop_loss_pct": self.stop_loss_pct,
                "take_profit_pct": self.take_profit_pct, "ladder": [list(r) for r in self.scale_out],
                "trail_arm_pct": self.trail_arm_pct, "trail_pct": self.trail_pct, "drop_m5_pct": self.drop_m5_pct,
                "derisk_pct": self.derisk_pct, "pump_m5_pct": self.pump_m5_pct, "pump_sell_pct": self.pump_sell_pct,
                "max_hold_hours": self.max_hold_hours}


def parse_ladder(raw):
    """SCALE_OUT="30:25,60:25,100:25": at +30% sell 25% of the original position, at +60% another 25%, ...
    "off" or empty = no ladder. The shares may not add up to more than 100%."""
    raw = str(raw or "").strip().lower()
    if raw in ("", "off", "0", "none"):
        return []
    rungs = []
    for part in raw.split(","):
        try:
            g, pct = (float(x) for x in part.split(":"))
        except ValueError:
            raise ValueError("SCALE_OUT must look like 30:25,60:25,100:25 (gain%:sell%, ...)") from None
        if not (math.isfinite(g) and math.isfinite(pct) and g > 0 and 0 < pct <= 100):
            raise ValueError("SCALE_OUT: each step is gain% > 0 : sell% between 1 and 100")
        rungs.append((g, pct))
    rungs.sort()
    if sum(p for _, p in rungs) > 100.0001:
        raise ValueError("SCALE_OUT sells more than 100% in total")
    return rungs


class State:
    """Everything the dashboard shows and the desk must survive a restart with, in one JSON file."""

    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        self.data = {"position": None, "positions": [], "trades": [], "equity": [], "paused": False, "pause_reason": None,
                     "day": None, "day_start_bank": None, "realized_today": 0.0, "agents": {}, "errors": [],
                     "judge": [], "cycle": None, "shadow": [], "started_at": _now(),
                     "feed": [], "working": None, "tests": [], "totals": {}, "lens": None, "pairs": {}}
        try:
            with open(path) as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass
        if not self.data.get("positions") and self.data.get("position"):   # a state file from the one-coin desk
            self.data["positions"] = [self.data["position"]]
        self.sync()

    def sync(self):
        """data["position"] is the first open position, kept for everything that shows one coin."""
        with self.lock:
            ps = self.data.setdefault("positions", [])
            self.data["position"] = ps[0] if ps else None

    def positions(self):
        with self.lock:
            return list(self.data.get("positions") or [])

    def find(self, pos):
        """The stored position for `pos` (same order, else same mint)."""
        with self.lock:
            for p in self.data.get("positions") or []:     # one coin is one position: the mint decides
                if p.get("mint") == pos.get("mint"):
                    return p
        return None

    def put(self, pos):
        with self.lock:
            ps = self.data.setdefault("positions", [])
            cur = self.find(pos)
            if cur is None:
                ps.append(pos)
            elif cur is not pos:
                ps[ps.index(cur)] = pos
            self.sync()

    def drop(self, pos):
        with self.lock:
            cur = self.find(pos)
            if cur is not None:
                self.data["positions"].remove(cur)
            self.sync()
            return not self.data["positions"]

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(tmp, "w") as fh:
                    json.dump(self.data, fh, default=str)
                os.replace(tmp, self.path)
            except OSError as exc:
                log.warning("could not save state: %s", exc)

    def agent(self, name, line, value=None, label=None):
        with self.lock:
            self.data["agents"][name] = {"line": str(line)[:160], "value": _f(value), "label": label, "updated": _now()}

    def feed(self, agent, kind, text, ticker=None, mint=None, check=None, no=None, ms=None):
        """One line of the live feed: which coin, which seat, what happened. Newest first."""
        row = {"t": _now(), "agent": agent, "kind": kind, "text": str(text)[:160]}
        if ticker:
            row["ticker"] = str(ticker)[:16]
        if isinstance(mint, str) and 32 <= len(mint) <= 44:
            row["mint"] = mint
        if check:
            row["check"] = check
        if isinstance(no, int):
            row["no"] = no
        if isinstance(ms, int):
            row["ms"] = ms
        with self.lock:
            self.data["feed"] = ([row] + (self.data.get("feed") or []))[:FEED_MAX]

    def bump(self, name, by=1):
        """Running totals since the desk started (survive restarts with the state file)."""
        with self.lock:
            totals = self.data.setdefault("totals", {})
            totals[name] = int(totals.get(name) or 0) + by
            return totals[name]

    def lens(self, t, stage, killed, no=None, answers=None):
        """The coin under the lens: its real numbers next to every threshold, for thresholds.py on the page.
        A number the desk doesn't have is None and shows as unknown, never as a pass."""
        f = lambda k: _f(t.get(k))
        age, top_w, top_10 = f("age_minutes"), f("top_wallet_percent"), f("top_10_percent")
        facts = {"age_minutes": age, "liquidity_usd": f("liquidity_usd"), "volume_h24": f("volume_h24"),
                 "mcap_usd": f("mcap_usd"), "price_usd": f("price_usd"), "holders": f("holder_count"),
                 "trades_h24": f("trades_h24"), "top_wallet": None if top_w is None else top_w / 100,
                 "top_10": None if top_10 is None else top_10 / 100,
                 "change_1h": _f((t.get("change") or {}).get("1h")), "change_24h": _f((t.get("change") or {}).get("24h"))}
        H = thresholds.HARD
        rule = {"min_age_minutes": ("age_minutes", "min"), "max_age_hours": ("age_minutes", "max_h"),
                "min_liquidity_usd": ("liquidity_usd", "min"), "min_volume_h24": ("volume_h24", "min"),
                "min_mcap_usd": ("mcap_usd", "min"), "max_mcap_usd": ("mcap_usd", "max"),
                "min_trades_h24": ("trades_h24", "min"), "max_top_wallet": ("top_wallet", "max"),
                "max_top_10": ("top_10", "max"), "min_holders": ("holders", "min")}
        hard = []
        for k, limit in H.items():
            fact, how = rule.get(k, (None, None))
            v = facts.get(fact) if fact else None
            if v is None or how is None:
                ok = None
            elif how == "min":
                ok = v >= limit
            elif how == "max":
                ok = v <= limit
            else:
                ok = v <= limit * 60
            hard.append({"k": k, "limit": limit, "v": v, "unit": "min" if fact == "age_minutes" else None, "ok": ok})
        soft = []
        for k, (direction, limit) in thresholds.SOFT.items():
            a = (answers or {}).get(k) if answers else None
            v = _f(a.get("noul", a.get("score"))) if isinstance(a, dict) else None
            ok = None if v is None else (v <= limit if direction == "max" else v >= limit)
            soft.append({"k": k, "dir": direction, "limit": limit, "v": v, "ok": ok})
        pair = t.get("pair_address")
        with self.lock:
            if isinstance(pair, str) and 32 <= len(pair) <= 44 and t.get("addr"):
                pairs = self.data.setdefault("pairs", {})
                pairs[t["addr"]] = pair
                if len(pairs) > 300:
                    for k in list(pairs)[:len(pairs) - 300]:
                        pairs.pop(k, None)
            prev = self.data.get("lens") or {}
            if prev.get("mint") == t.get("addr") and answers is None:
                soft = prev.get("soft") or soft            # keep Jev's numbers while the same coin moves on
            self.data["lens"] = {"time": _now(), "no": no, "ticker": str(t.get("ticker") or "")[:16] or None,
                                 "mint": t.get("addr"), "pair": pair if isinstance(pair, str) else None, "stage": stage, "verdict": killed or "passed",
                                 "facts": facts, "hard": hard, "soft": soft}

    def set_working(self, agent=None, step=None, ticker=None, mint=None, n=None, of=None):
        """What the desk is doing this second, for the floor. agent=None: nothing (between scans)."""
        with self.lock:
            self.data["working"] = None if agent is None else {
                "agent": agent, "step": step, "ticker": str(ticker)[:16] if ticker else None,
                "mint": mint if isinstance(mint, str) and 32 <= len(mint) <= 44 else None,
                "n": n, "of": of, "since": _now()}

    def error(self, text):
        with self.lock:
            self.data["errors"] = ([{"time": _now(), "text": str(text)[:300]}] + self.data["errors"])[:20]
        log.warning("%s", text)


def record(trades):
    """Wins and losses per position: the sales of one order (its ladder steps and its last sale) add up to one result."""
    by = {}
    for i, t in enumerate(trades or []):
        key = t.get("order_id") or f"row{i}"
        b = by.setdefault(key, {"pnl": 0.0, "closed": False})
        b["pnl"] += t.get("pnl_usd") or 0.0
        b["closed"] = b["closed"] or t.get("final", True)
    closed = [b["pnl"] for b in by.values() if b["closed"]]
    return sum(1 for x in closed if x > 0), sum(1 for x in closed if x < 0)


def volume_ratio(volume: dict, age_minutes):
    """RISK's rule, made to hold for young tokens too. For a token at least 24 h old this is exactly the desk's
    rule: volume.h6 / (volume.h24 / 4). A younger token has less than 24 h inside its h24 number, and under
    6 h its h6 equals its h24 (the ratio would read 4 and never close), so the window shrinks with age:
    6 h once there are 12 h of history, 1 h from 2 h, 5 min below that, each against the average over the
    token's real lifetime. Returns (ratio, window) or (None, why)."""
    age = _f(age_minutes)
    if age is None:
        return None, "pool age unknown"
    hours = min(age / 60.0, 24.0)
    if hours <= 0:
        return None, "pool age is zero"
    window, key = (6.0, "h6") if hours >= 12 else (1.0, "h1") if hours >= 2 else (5 / 60, "m5")
    recent, h24 = _f((volume or {}).get(key)), _f((volume or {}).get("h24"))
    if recent is None or h24 is None:
        return None, f"volume {key} or h24 not reported"
    avg = h24 * window / hours
    if avg <= 0:
        return None, "no volume over the token's life"
    return recent / avg, key


class Seats:
    def __init__(self, cfg: Config, jupiter, rpc, keypair, state: State, sleep=time.sleep):
        self.cfg, self.jup, self.rpc, self.kp, self.state, self._sleep = cfg, jupiter, rpc, keypair, state, sleep
        if cfg.live and keypair is None:
            raise ValueError("LIVE_MODE=true needs BOT_WALLET_KEY")

    # ---------- the bank ----------
    def sol_usd(self):
        try:
            return self.jup.sol_usd()
        except Exception as exc:
            self.state.error(f"SOL price unavailable: {exc}")
            return None

    def bank(self):
        """Free cash in USD, or None if it can't be known (then the desk does not scan)."""
        if not self.cfg.live:                    # paper cash: start + realized P&L - what the open position cost
            with self.state.lock:
                realized = sum(t.get("pnl_usd") or 0.0 for t in self.state.data["trades"] if t.get("mode") == "paper")
                open_cost = sum((p.get("cost_usd") or 0.0) for p in self.state.data.get("positions") or []
                                if p.get("mode") == "paper")
            return round(self.cfg.paper_bank_usd + realized - open_cost, 2)
        price = self.sol_usd()
        try:
            lamports = self.rpc.sol_lamports(self.kp.address)
        except Exception as exc:
            self.state.error(f"wallet balance unavailable: {exc}")
            return None
        if price is None:
            return None
        free = max(0.0, lamports / LAMPORTS - self.cfg.fee_reserve_sol)
        return round(free * price, 2)

    def daily_guard(self, bank):
        """Stops new entries once today's realized loss reaches MAX_DAILY_LOSS_PCT of the day's opening bank."""
        day = datetime.now(timezone.utc).date().isoformat()
        with self.state.lock:
            d = self.state.data
            if d["day"] != day:
                d["day"], d["day_start_bank"], d["realized_today"] = day, bank, 0.0
            start, lost = d["day_start_bank"], -(d["realized_today"] or 0.0)
            limit = self.cfg.max_daily_loss_pct
            if limit > 0 and _f(start) and lost >= start * limit / 100:
                d["paused"], d["pause_reason"] = True, f"daily loss limit: -${lost:,.2f} today ({limit:g}% of ${start:,.2f})"
                return False
        return True

    # ---------- SIZE ----------
    def size(self, order, bank):
        facts = order.get("facts") or {}
        bank, liq, factor = _f(bank), _f(facts.get("liquidity_usd")), _f(order.get("size_factor"))
        if bank is None:
            return 0.0, "no bank figure"
        if liq is None:
            return 0.0, "pool liquidity unknown"
        if factor is None:
            return 0.0, "no size factor on the order"
        ticket = min(bank * TICKET_SHARE * factor, liq * MAX_POOL_SHARE)
        open_cost = sum(_f(p.get("cost_usd")) or 0.0 for p in self.state.positions())
        room = (bank + open_cost) * self.cfg.max_exposure_pct / 100 - open_cost
        if ticket > room:                        # all coins together stay under MAX_EXPOSURE_PCT of everything
            if room < self.cfg.min_ticket_usd:
                return 0.0, (f"${open_cost:,.2f} already in coins: the {self.cfg.max_exposure_pct:g}% exposure cap "
                             "leaves no room")
            ticket = room
        if ticket < self.cfg.min_ticket_usd:
            return 0.0, f"ticket ${ticket:,.2f} is under the ${self.cfg.min_ticket_usd:g} floor"
        return round(ticket, 2), None

    # ---------- FILLS ----------
    def buy(self, order, ticket_usd):
        """Returns the position dict, or raises with the reason nothing was bought."""
        t, facts = order["token"], order.get("facts") or {}
        if t.get("chain") != "solana":
            raise RuntimeError(f"{t.get('chain')} can't be traded through Jupiter (Solana only)")
        price, decimals = _f(facts.get("price_usd")), facts.get("decimals")
        if price is None or not isinstance(decimals, int):
            raise RuntimeError("price or token decimals unknown: price impact can't be checked")
        sol_usd = self.sol_usd()
        if sol_usd is None:
            raise RuntimeError("SOL price unknown")
        lamports = int(ticket_usd / sol_usd * LAMPORTS)
        if self.cfg.live:
            have = self.rpc.sol_lamports(self.kp.address)
            if have < lamports + int(self.cfg.fee_reserve_sol * LAMPORTS):
                raise RuntimeError(f"wallet holds {have / LAMPORTS:.4f} SOL, needs {lamports / LAMPORTS:.4f} + fee reserve")
        q = self.jup.order(WSOL, t["address"], lamports, taker=self.kp.address if self.cfg.live else None)
        expected = ticket_usd / price
        got = int(q["outAmount"]) / 10 ** decimals
        impact = (1 - got / expected) * 100 if expected > 0 else None
        if impact is None or impact >= self.cfg.max_impact_pct:
            raise RuntimeError(f"price impact {impact:.2f}% is at or over the {self.cfg.max_impact_pct:g}% limit"
                               if impact is not None else "price impact can't be computed")
        position = {"order_id": order["order_id"], "mode": "live" if self.cfg.live else "paper",
                    "ticker": t.get("ticker"), "mint": t["address"], "chain": "solana", "decimals": decimals,
                    "opened_at": _now(), "opened_ts": time.time(), "ticket_usd": ticket_usd,
                    "sol_usd_at_entry": sol_usd, "impact_pct": round(impact, 3), "status": "open",
                    "model": order.get("model"), "size_factor": order.get("size_factor")}
        if self.cfg.live:
            with self.state.lock:                    # write-ahead: a crash after sending is never a lost position
                self.state.put({**position, "status": "sending", "tokens_raw": None,
                                "spent_lamports": lamports, "request_id": q.get("requestId")})
                self.state.save()
            signed = sign_transaction(q["transaction"], self.kp)
            try:
                res = self.jup.execute(signed, q["requestId"])
            except Exception as exc:
                definite = isinstance(exc, JupiterError) and (exc.code is not None or exc.status in (400, 401, 403, 429))
                with self.state.lock:
                    if definite:                     # Jupiter says it did not happen
                        self.state.drop(position)
                    else:                            # timeout, network, 5xx: it may have landed. RISK reads the chain.
                        self.state.find(position)["status"] = "unconfirmed"
                    self.state.save()
                raise RuntimeError(f"swap not filled: {exc}" if definite
                                   else f"swap outcome unknown, RISK will check the wallet: {exc}") from None
            tokens = int(res.get("totalOutputAmount") or res.get("outputAmountResult") or 0)
            spent = int(res.get("totalInputAmount") or res.get("inputAmountResult") or lamports)
            position.update(tokens_raw=tokens, spent_lamports=spent, signature=res.get("signature"))
        else:
            position.update(tokens_raw=int(q["outAmount"]), spent_lamports=lamports, signature=None)
        cost_usd = position["spent_lamports"] / LAMPORTS * sol_usd
        position["cost_usd"] = round(cost_usd, 2)
        position["entry_price_usd"] = cost_usd / (position["tokens_raw"] / 10 ** decimals) if position["tokens_raw"] else None
        position.update(tokens_raw_initial=position["tokens_raw"], cost_usd_initial=position["cost_usd"], realized_usd=0.0,
                        peak_price=position["entry_price_usd"], rungs_done=[], sells=0)
        supply = supply_from(facts.get("mcap_usd"), facts.get("price_usd"))
        entry = position["entry_price_usd"]
        position.update(supply=supply, entry_mcap=entry * supply if supply and entry else None,
                        mcap_now=_f(facts.get("mcap_usd")), peak_mcap=entry * supply if supply and entry else None)
        with self.state.lock:
            self.state.put(position)
            self.state.save()
        return position

    def send_to_seats(self, order, bank):
        """SIZE, then FILLS. If nothing is bought, RISK is told so and frees the book (it is still RISK's call)."""
        ticket, why = self.size(order, bank)
        tok = order["token"]
        label = tok.get("ticker") or tok["address"][:6]
        coin = {"ticker": tok.get("ticker"), "mint": tok.get("address")}
        if not ticket:
            self.state.agent("size", f"0 for {label}: {why}", 0, "ticket")
            self.state.feed("size", "skip", f"no ticket: {why}", **coin)
            return self.release_unfilled(order, f"SIZE: {why}")
        self.state.agent("size", f"{label}: ${ticket:,.2f} ({100 * ticket / bank:.1f}% of bank)", ticket, "ticket")
        self.state.feed("size", "info", f"ticket ${ticket:,.2f} ({100 * ticket / bank:.1f}% of bank)", **coin)
        self.state.set_working("fills", "buying", **coin)
        try:
            pos = self.buy(order, ticket)
        except Exception as exc:
            self.state.agent("fills", f"no fill on {label}: {exc}", None, "last fill")
            self.state.feed("fills", "error", f"no fill: {exc}", **coin)
            if (self.state.find({"order_id": order["order_id"], "mint": tok["address"]}) or {}).get("status") == "unconfirmed":
                return None                         # RISK reconciles it from the wallet
            return self.release_unfilled(order, f"FILLS: {exc}")
        self.state.agent("fills", f"in ${pos['cost_usd']:,.2f} on {label}, impact {pos['impact_pct']:.2f}%"
                         + (" (paper)" if pos["mode"] == "paper" else ""), pos["cost_usd"], "last fill")
        self.state.bump("buys")
        self.state.feed("fills", "buy", f"BOUGHT ${pos['cost_usd']:,.2f}"
                        + (" (paper)" if pos["mode"] == "paper" else " with real SOL")
                        + f", impact {pos['impact_pct']:.2f}%", **coin)
        self.state.agent("risk", f"watching {label}: first check in {int(self.cfg.risk_poll_seconds // 60)} min", None, "holding")
        return pos

    def test_swap(self, usd):
        """Proves the wallet can trade before any memecoin does: SOL -> USDC -> SOL for about `usd`.
        Paper: two real Jupiter quotes, nothing signed. Live: two real swaps, signed with the bot key.
        Returns a result row; raises only before anything was sent."""
        sol_usd = self.sol_usd()
        if sol_usd is None:
            raise RuntimeError("SOL price unknown: can't size the test")
        lamports = int(usd / sol_usd * LAMPORTS)
        if lamports <= 0:
            raise RuntimeError("test amount rounds to zero")
        live = self.cfg.live
        out = {"time": _now(), "mode": "live" if live else "paper", "usd": usd, "sol_in": lamports / LAMPORTS,
               "usdc": None, "sol_back": None, "sigs": [], "ok": False, "note": ""}
        if live:
            have = self.rpc.sol_lamports(self.kp.address)
            need = lamports + int(self.cfg.fee_reserve_sol * LAMPORTS)
            if have < need:
                raise RuntimeError(f"wallet holds {have / LAMPORTS:.4f} SOL; the test needs {need / LAMPORTS:.4f} "
                                   "including the fee reserve")
        taker = self.kp.address if live else None
        q1 = self.jup.order(WSOL, USDC, lamports, taker=taker)
        if not live:
            usdc = int(q1["outAmount"])
            q2 = self.jup.order(USDC, WSOL, usdc)
            out.update(usdc=usdc / 1e6, sol_back=int(q2["outAmount"]) / LAMPORTS, ok=True,
                       note="paper: real Jupiter quotes both ways, nothing signed or sent")
            return out
        r1 = self.jup.execute(sign_transaction(q1["transaction"], self.kp), q1["requestId"])
        out["sigs"].append(r1.get("signature"))
        try:
            usdc = int(r1.get("totalOutputAmount") or 0) or self.rpc.token_balance(self.kp.address, USDC)[0]
        except Exception:
            usdc = 0
        out["usdc"] = usdc / 1e6 if usdc else None
        if not usdc:
            out["note"] = "SOL -> USDC landed but the USDC amount couldn't be read: it is in the bot wallet"
            return out
        try:
            q2 = self.jup.order(USDC, WSOL, usdc, taker=taker)
            r2 = self.jup.execute(sign_transaction(q2["transaction"], self.kp), q2["requestId"])
        except Exception as exc:
            out["note"] = f"SOL -> USDC worked; USDC -> SOL failed ({exc}). The USDC is in the bot wallet."
            return out
        out["sigs"].append(r2.get("signature"))
        back = int(r2.get("totalOutputAmount") or 0)
        out.update(sol_back=back / LAMPORTS if back else None, ok=True,
                   note="live: both swaps landed. The wallet can trade.")
        return out

    def release_unfilled(self, order, why):
        if not self.state.positions():
            self.state.agent("risk", f"nothing filled, desk released ({why})", None, "released")
            book.release()
        return None

    # ---------- RISK ----------
    def _pair(self, mint):
        body = collect._get_json(f"{collect.DEX}/solana/{mint}")
        pairs = [p for p in (body or []) if isinstance(p, dict) and (p.get("baseToken") or {}).get("address") == mint]
        if not pairs:
            return None
        liq = lambda p: collect._num((p.get("liquidity") or {}).get("usd")) or 0.0
        return max(pairs, key=liq)

    def risk_poll(self):
        """One RISK check of every open position. Returns the first sale it made, or None."""
        ps = self.state.positions()
        if not ps:
            held = book.held()
            if held is None:
                return None
            pos = self._rebuild(held)                # state lost (redeploy without a volume): rebuild from the chain
            ps = [pos] if pos is not None else []
        sold = None
        for pos in ps:
            t = self.risk_one(pos)
            sold = sold or t
        return sold

    def risk_one(self, pos):
        """Close on the volume rule (or stop-loss), or when the position can't be measured three times running."""
        if pos.get("status") in ("unconfirmed", "sending"):
            return self._reconcile(pos)
        label = pos.get("ticker") or pos["mint"][:6]
        try:
            pair = self._pair(pos["mint"])
        except collect.FetchFailed as exc:
            pair, err = None, str(exc)
        else:
            err = "no pair on DexScreener"
        if pair is None:
            pos["misses"] = int(pos.get("misses") or 0) + 1
            if pos["misses"] < 3:                    # no answer: retry twice, then close anyway
                self.state.agent("risk", f"{label}: no data ({err}), retry {pos['misses']}/2", None, "holding")
                return None
            return self.close(pos, "can't measure the position three times running")
        pos["misses"] = 0
        age = None
        created = collect._num(pair.get("pairCreatedAt"))
        if created:
            age = (time.time() - (created / 1000 if created > 1e11 else created)) / 60
        ratio, window = volume_ratio(pair.get("volume") or {}, age)
        price = collect._num(pair.get("priceUsd"))
        value = price * pos["tokens_raw"] / 10 ** pos["decimals"] if price is not None and pos.get("tokens_raw") else None
        pnl = (value - pos["cost_usd"]) if value is not None and _f(pos.get("cost_usd")) is not None else None
        supply = supply_from(pair.get("marketCap"), price)
        with self.state.lock:
            p = self.state.find(pos) or pos
            p.update(last_value_usd=value, last_pnl_usd=pnl, last_ratio=ratio, last_check=_now())
            if supply:
                p.update(supply=supply, mcap_now=round(price * supply, 2))
        if ratio is None:
            pos["misses"] = int(pos.get("misses") or 0) + 1
            if pos["misses"] < 3:
                self.state.agent("risk", f"{label}: {window}, retry {pos['misses']}/2", None, "holding")
                return None
            return self.close(pos, f"can't measure volume three times running ({window})")
        line = f"{label}: volume ratio {ratio:.2f} ({window})" + (f", P&L ${pnl:+,.2f}" if pnl is not None else "")
        self.state.agent("risk", line, ratio, "holding")
        if ratio < 0.20:
            return self.close(pos, f"volume ratio {ratio:.2f} < 0.20 ({window})")
        sl = self.cfg.stop_loss_pct
        if sl > 0 and value is not None and value <= pos["cost_usd"] * (1 - sl / 100):
            return self.close(pos, f"stop-loss: value ${value:,.2f} is {sl:g}% under cost")
        return None

    def close(self, pos, why):
        return self.sell(pos, 1.0, why)

    def sell(self, pos, fraction, why):
        """Sell `fraction` of what is held now (1.0 = everything, which closes the position and frees the book).
        Each sale is its own row in trades, with the share of cost it used up; the rest of the cost stays on the
        position. A part sale that would leave dust sells everything instead. Returns the trade row, or None
        when nothing could be sold (it is tried again on the next check)."""
        label = pos.get("ticker") or pos["mint"][:6]
        held = int(pos.get("tokens_raw") or 0)
        full = fraction >= 0.999
        if not full:
            left = _f(pos.get("last_value_usd"))
            if held <= 0 or int(held * fraction) <= 0 or (left is not None and left * (1 - fraction) < max(1.0, self.cfg.min_ticket_usd / 2)):
                full = True
        sol_usd = self.sol_usd()
        if sol_usd is None and pos["mode"] == "paper":   # a paper P&L can't be known without the SOL price: wait,
            self.state.agent("risk", f"sale of {label} waits: SOL price unknown", None, "closing")   # don't book 0
            return None
        try:
            if pos["mode"] == "live":
                balance, _ = self.rpc.token_balance(self.kp.address, pos["mint"])
                amount = balance if full else min(balance, int(balance * fraction))
                if amount <= 0:
                    proceeds_lamports = 0
                else:
                    q = self.jup.order(pos["mint"], WSOL, amount, taker=self.kp.address)
                    res = self.jup.execute(sign_transaction(q["transaction"], self.kp), q["requestId"])
                    proceeds_lamports = int(res.get("totalOutputAmount") or res.get("outputAmountResult") or 0)
                held = max(held, balance)
            else:
                amount = held if full else int(held * fraction)
                q = self.jup.order(pos["mint"], WSOL, int(amount))
                proceeds_lamports = int(q["outAmount"])
        except Exception as exc:
            self.state.agent("risk", f"sale of {label} failed, retrying: {exc}", None, "closing")
            self.state.error(f"RISK sale failed for {label}: {exc}")
            return None                              # still held; the next check tries again
        share = 1.0 if full or not held else amount / held
        proceeds_usd = proceeds_lamports / LAMPORTS * sol_usd if sol_usd is not None else None
        basis = _f(pos.get("cost_usd"))
        cost_part = basis * share if basis is not None else None
        pnl = round(proceeds_usd - cost_part, 2) if proceeds_usd is not None and cost_part is not None else None
        initial = int(pos.get("tokens_raw_initial") or held or 1)
        trade = {"order_id": pos.get("order_id"), "mode": pos["mode"], "ticker": pos.get("ticker"), "mint": pos["mint"],
                 "opened_at": pos.get("opened_at"), "closed_at": _now(),
                 "cost_usd": round(cost_part, 2) if cost_part is not None else None,
                 "proceeds_usd": round(proceeds_usd, 2) if proceeds_usd is not None else None, "pnl_usd": pnl,
                 "proceeds_sol": proceeds_lamports / LAMPORTS, "reason": why, "model": pos.get("model"),
                 "final": full, "sold_pct": round(100 * amount / initial, 1) if initial else None,
                 "mcap_in": _f(pos.get("entry_mcap")),
                 "mcap_out": (round(proceeds_usd / (amount / 10 ** pos["decimals"]) * pos["supply"], 2)
                              if proceeds_usd is not None and amount and _f(pos.get("supply")) and pos.get("decimals") is not None
                              else None)}
        with self.state.lock:
            d = self.state.data
            d["trades"] = ([trade] + d["trades"])[:200]
            flat = False
            if full:
                flat = self.state.drop(pos)
            else:
                p = self.state.find(pos) or pos
                p["tokens_raw"] = held - amount
                p["cost_usd"] = round(basis - cost_part, 2) if basis is not None else None
                p["realized_usd"] = round((p.get("realized_usd") or 0.0) + (pnl or 0.0), 2)
                p["sells"] = int(p.get("sells") or 0) + 1
                if _f(p.get("last_value_usd")) is not None:
                    p["last_value_usd"] = round(p["last_value_usd"] * (1 - share), 2)
                if p.get("cost_usd") is not None and _f(p.get("last_value_usd")) is not None:
                    p["last_pnl_usd"] = round(p["last_value_usd"] - p["cost_usd"], 2)
            if pnl is not None and d.get("day") == datetime.now(timezone.utc).date().isoformat():
                d["realized_today"] = (d.get("realized_today") or 0.0) + pnl
            self.state.save()
        wins, losses = record(self.state.data["trades"])
        part = "" if full else f" on {round(100 * share)}% of the bag"
        money = f"{pnl:+,.2f} USD" if pnl is not None else "(P&L unknown)"
        self.state.agent("risk", (f"closed {label}: {why}" if full else f"sold {round(100 * share)}% of {label}: {why}")
                         + (f", ${pnl:+,.2f}" if pnl is not None else ", P&L unknown"), pnl, f"{wins}W {losses}L")
        self.state.bump("sells")
        self.state.feed("risk", "sell", f"SOLD {money}{part}" + (" (paper)" if pos["mode"] == "paper" else "") + f": {why}",
                        pos.get("ticker"), pos.get("mint"))
        if full:
            book.sit(f"{pos['mint']}:{collect.FOMO_NET['solana']}", "sold")   # no buying straight back in
            if flat:
                book.release()                       # the last coin sold frees the book
        return trade

    # ---------- WATCH: the price manager, every WATCH_SECONDS while a position is open ----------
    def watch_poll(self, clock=time.time):
        """Reads the coin's real price and its last five minutes of buys and sells, keeps the peak since entry, and
        applies the rules in this order: stop-loss, take-profit, max hold, trailing stop, the profit ladder,
        breaking down, turning down, spike. One action per check. A number it can't read means no action."""
        if not self.cfg.watch:
            return None
        sold = None
        for pos in self.state.positions():
            t = self.watch_one(pos, clock)
            sold = sold or t
        return sold

    def watch_one(self, pos, clock=time.time):
        if not pos or pos.get("status") != "open" or not pos.get("tokens_raw"):
            return None
        label = pos.get("ticker") or pos["mint"][:6]
        try:
            pair = self._pair(pos["mint"])
        except collect.FetchFailed as exc:
            self.state.agent("risk", f"watch {label}: no price right now ({exc})", None, "watching")
            return None
        price = collect._num((pair or {}).get("priceUsd"))
        entry = _f(pos.get("entry_price_usd"))
        if price is None or entry is None or entry <= 0:
            self.state.agent("risk", f"watch {label}: price unknown, no action", None, "watching")
            return None
        c = self.cfg
        now = clock()
        peak = max(_f(pos.get("peak_price")) or price, price)
        gain, peak_gain = (price / entry - 1) * 100, (peak / entry - 1) * 100
        off_peak = (1 - price / peak) * 100 if peak > 0 else 0.0
        m5 = collect._num(((pair.get("priceChange") or {}).get("m5")))
        tx = ((pair.get("txns") or {}).get("m5") or {})
        buys, sells = collect._num(tx.get("buys")), collect._num(tx.get("sells"))
        tokens = int(pos["tokens_raw"]) / 10 ** pos["decimals"]
        value = price * tokens
        basis = _f(pos.get("cost_usd"))
        supply = supply_from((pair or {}).get("marketCap"), price) or _f(pos.get("supply"))
        with self.state.lock:
            p = self.state.find(pos) or pos
            if supply:
                p.update(supply=supply, mcap_now=round(price * supply, 2), peak_mcap=round(peak * supply, 2))
                if not _f(p.get("entry_mcap")):
                    p["entry_mcap"] = round(entry * supply, 2)
            p.update(last_price=price, peak_price=peak, gain_pct=round(gain, 2), peak_gain_pct=round(peak_gain, 2),
                     off_peak_pct=round(off_peak, 2), m5_change=m5, m5_buys=buys, m5_sells=sells,
                     last_value_usd=round(value, 2), last_pnl_usd=round(value - basis, 2) if basis is not None else None,
                     watched_at=_now())
        age_h = (now - (pos.get("opened_ts") or now)) / 3600
        done = set(pos.get("rungs_done") or [])
        sellers = buys is not None and sells is not None
        act = None
        if c.stop_loss_pct > 0 and gain <= -c.stop_loss_pct:
            act = (1.0, f"stop-loss at {gain:+.1f}% (limit -{c.stop_loss_pct:g}%)", None)
        elif c.take_profit_pct > 0 and gain >= c.take_profit_pct:
            act = (1.0, f"take-profit at {gain:+.1f}% (target +{c.take_profit_pct:g}%)", None)
        elif c.max_hold_hours > 0 and age_h >= c.max_hold_hours:
            act = (1.0, f"held {age_h:.1f}h, the {c.max_hold_hours:g}h limit, at {gain:+.1f}%", None)
        elif c.trail_pct > 0 and peak_gain >= c.trail_arm_pct and off_peak >= c.trail_pct:
            act = (1.0, f"trailing stop: {off_peak:.1f}% off the peak (best {peak_gain:+.1f}%, now {gain:+.1f}%): looks like the top", None)
        else:
            for g, pct in c.scale_out:
                if g not in done and gain >= g:
                    share = min(1.0, pct / 100 * int(pos.get("tokens_raw_initial") or pos["tokens_raw"]) / int(pos["tokens_raw"]))
                    act = (share, f"take profit at +{g:g}% (now {gain:+.1f}%): {pct:g}% of the original bag", ("rung", g))
                    break
        if act is None and sellers and m5 is not None:
            if gain <= 0 and c.drop_m5_pct > 0 and m5 <= -2 * c.drop_m5_pct and sells > buys * 1.5:
                act = (1.0, f"breaking down: {m5:+.1f}% in 5 min, {int(sells)} sells vs {int(buys)} buys, at {gain:+.1f}%", None)
            elif (gain > 0 and c.drop_m5_pct > 0 and c.derisk_pct > 0 and m5 <= -c.drop_m5_pct and sells > buys * 1.3
                  and now - (pos.get("derisk_ts") or 0) > 600):
                act = (c.derisk_pct / 100, f"turning down: {m5:+.1f}% in 5 min, {int(sells)} sells vs {int(buys)} buys: "
                                           f"locking in {c.derisk_pct:g}% at {gain:+.1f}%", ("derisk", now))
            elif (gain >= 20 and c.pump_m5_pct > 0 and c.pump_sell_pct > 0 and m5 >= c.pump_m5_pct
                  and now - (pos.get("spike_ts") or 0) > 900):
                act = (c.pump_sell_pct / 100, f"spike: {m5:+.1f}% in 5 min at {gain:+.1f}%: taking {c.pump_sell_pct:g}% off",
                       ("spike", now))
        if act is None:
            nxt = next((g for g, _ in c.scale_out if g not in done), None)
            trail = (f"trail armed, sells at -{c.trail_pct:g}% off the peak" if peak_gain >= c.trail_arm_pct
                     else f"trail arms at +{c.trail_arm_pct:g}%")
            self.state.agent("risk", f"watching {label}: MC {mc(price * supply) if supply else 'unknown'} "
                                     f"({gain:+.1f}%, best {peak_gain:+.1f}%) · {trail}"
                             + (f" · next take-profit at +{nxt:g}%" if nxt is not None else ""), gain / 100, "gain")
            return None
        share, why, mark = act
        trade = self.sell(pos, share, why)
        if trade is not None and mark is not None and not trade.get("final"):
            with self.state.lock:
                p = self.state.find(pos)
                if p is not None:
                    if mark[0] == "rung":
                        p["rungs_done"] = sorted(set(p.get("rungs_done") or []) | {mark[1]})
                    else:
                        p[f"{mark[0]}_ts"] = mark[1]
                    self.state.save()
        return trade

    def _rebuild(self, held):
        """The state file was lost but the book says we hold something: read the wallet, never guess the entry."""
        tokens, decimals = (self.rpc.token_balance(self.kp.address, held_addr(held)) if self.cfg.live and self.kp
                            else (0, None))
        pos = {"order_id": None, "mode": "live" if self.cfg.live else "paper", "ticker": held.get("ticker"),
               "mint": held_addr(held), "chain": "solana", "decimals": decimals, "tokens_raw": tokens,
               "cost_usd": None, "status": "open", "opened_at": None, "rebuilt": True}
        if not tokens or decimals is None:
            self.state.agent("risk", f"book held {held.get('ticker')} but the wallet holds none: released", None, "released")
            book.release()
            return None
        with self.state.lock:
            self.state.put(pos)
            self.state.save()
        return pos

    def _reconcile(self, pos):
        amount, decimals = self.rpc.token_balance(self.kp.address, pos["mint"])
        with self.state.lock:
            if amount > 0:
                pos.update(status="open", tokens_raw=amount, decimals=decimals or pos.get("decimals"), cost_usd=None,
                           tokens_raw_initial=amount)
                self.state.put(pos)
                self.state.agent("risk", f"{pos.get('ticker')}: swap landed after all, holding (entry cost unknown)", None, "holding")
            else:
                if self.state.drop(pos):
                    book.release()
                self.state.agent("risk", f"{pos.get('ticker')}: swap never landed, nothing held", None, "released")
            self.state.save()
        return None


def held_addr(held):
    r = book.DB.execute("SELECT addr FROM position WHERE id=1").fetchone()
    return r[0] if r else None
