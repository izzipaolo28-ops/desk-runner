# seats.py
"""SIZE, FILLS and RISK, in code. Arithmetic stays in code; nobody re-reads `why`.

Paper mode (LIVE_MODE=false, the default): the whole loop runs on real Jupiter quotes and real prices, but
nothing is signed or sent. Live mode signs with the bot's own wallet and lets Jupiter land the swap.
One position at a time. RISK alone closes it and alone calls book.release().
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
from jupiter import WSOL, JupiterError
from main import TICKET_SHARE
from solana_wallet import sign_transaction

log = logging.getLogger("seats")
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
        self.stop_loss_pct = _env_num(env, "STOP_LOSS_PCT", 0, high=100)    # 0 = off: RISK's volume rule only
        self.risk_poll_seconds = _env_num(env, "RISK_POLL_SECONDS", 300, low=30)
        self.data_dir = env.get("DATA_DIR", "data")


class State:
    """Everything the dashboard shows and the desk must survive a restart with, in one JSON file."""

    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        self.data = {"position": None, "trades": [], "equity": [], "paused": False, "pause_reason": None,
                     "day": None, "day_start_bank": None, "realized_today": 0.0, "agents": {}, "errors": [],
                     "judge": [], "cycle": None, "shadow": [], "started_at": _now()}
        try:
            with open(path) as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass

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

    def error(self, text):
        with self.lock:
            self.data["errors"] = ([{"time": _now(), "text": str(text)[:300]}] + self.data["errors"])[:20]
        log.warning("%s", text)


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
        self.data_misses = 0

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
                pos = self.state.data.get("position") or {}
                open_cost = pos.get("cost_usd") or 0.0 if pos.get("mode") == "paper" else 0.0
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
                self.state.data["position"] = {**position, "status": "sending", "tokens_raw": None,
                                               "spent_lamports": lamports, "request_id": q.get("requestId")}
                self.state.save()
            signed = sign_transaction(q["transaction"], self.kp)
            try:
                res = self.jup.execute(signed, q["requestId"])
            except Exception as exc:
                definite = isinstance(exc, JupiterError) and (exc.code is not None or exc.status in (400, 401, 403, 429))
                with self.state.lock:
                    if definite:                     # Jupiter says it did not happen
                        self.state.data["position"] = None
                    else:                            # timeout, network, 5xx: it may have landed. RISK reads the chain.
                        self.state.data["position"]["status"] = "unconfirmed"
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
        with self.state.lock:
            self.state.data["position"] = position
            self.state.save()
        return position

    def send_to_seats(self, order, bank):
        """SIZE, then FILLS. If nothing is bought, RISK is told so and frees the book (it is still RISK's call)."""
        ticket, why = self.size(order, bank)
        label = order["token"].get("ticker") or order["token"]["address"][:6]
        if not ticket:
            self.state.agent("size", f"0 for {label}: {why}", 0, "ticket")
            return self.release_unfilled(order, f"SIZE: {why}")
        self.state.agent("size", f"{label}: ${ticket:,.2f} ({100 * ticket / bank:.1f}% of bank)", ticket, "ticket")
        try:
            pos = self.buy(order, ticket)
        except Exception as exc:
            self.state.agent("fills", f"no fill on {label}: {exc}", None, "last fill")
            if (self.state.data.get("position") or {}).get("status") == "unconfirmed":
                return None                         # RISK reconciles it from the wallet
            return self.release_unfilled(order, f"FILLS: {exc}")
        self.state.agent("fills", f"in ${pos['cost_usd']:,.2f} on {label}, impact {pos['impact_pct']:.2f}%"
                         + (" (paper)" if pos["mode"] == "paper" else ""), pos["cost_usd"], "last fill")
        self.state.agent("risk", f"watching {label}: first check in {int(self.cfg.risk_poll_seconds // 60)} min", None, "holding")
        return pos

    def release_unfilled(self, order, why):
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
        """One RISK check: close on the volume rule (or stop-loss, if set), or when the position can't be measured
        three times running. Returns the closed trade, or None while still holding."""
        with self.state.lock:
            pos = self.state.data.get("position")
        held = book.held()
        if pos is None and held is None:
            return None
        if pos is None:                              # state lost (redeploy without a volume): rebuild from the chain
            pos = self._rebuild(held)
            if pos is None:
                return None
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
            self.data_misses += 1
            if self.data_misses < 3:                 # no answer: retry twice, then close anyway
                self.state.agent("risk", f"{label}: no data ({err}), retry {self.data_misses}/2", None, "holding")
                return None
            return self.close(pos, "can't measure the position three times running")
        self.data_misses = 0
        age = None
        created = collect._num(pair.get("pairCreatedAt"))
        if created:
            age = (time.time() - (created / 1000 if created > 1e11 else created)) / 60
        ratio, window = volume_ratio(pair.get("volume") or {}, age)
        price = collect._num(pair.get("priceUsd"))
        value = price * pos["tokens_raw"] / 10 ** pos["decimals"] if price is not None and pos.get("tokens_raw") else None
        pnl = (value - pos["cost_usd"]) if value is not None and _f(pos.get("cost_usd")) is not None else None
        with self.state.lock:
            p = self.state.data.get("position") or {}
            p.update(last_value_usd=value, last_pnl_usd=pnl, last_ratio=ratio, last_check=_now())
        if ratio is None:
            self.data_misses += 1
            if self.data_misses < 3:
                self.state.agent("risk", f"{label}: {window}, retry {self.data_misses}/2", None, "holding")
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
        label = pos.get("ticker") or pos["mint"][:6]
        sol_usd = self.sol_usd()
        if sol_usd is None and pos["mode"] == "paper":   # a paper P&L can't be known without the SOL price: wait,
            self.state.agent("risk", f"close of {label} waits: SOL price unknown", None, "closing")   # don't book 0
            return None
        try:
            if pos["mode"] == "live":
                amount, _ = self.rpc.token_balance(self.kp.address, pos["mint"])
                if amount <= 0:
                    proceeds_lamports = 0
                else:
                    q = self.jup.order(pos["mint"], WSOL, amount, taker=self.kp.address)
                    res = self.jup.execute(sign_transaction(q["transaction"], self.kp), q["requestId"])
                    proceeds_lamports = int(res.get("totalOutputAmount") or res.get("outputAmountResult") or 0)
            else:
                q = self.jup.order(pos["mint"], WSOL, int(pos["tokens_raw"]))
                proceeds_lamports = int(q["outAmount"])
        except Exception as exc:
            self.state.agent("risk", f"close of {label} failed, retrying: {exc}", None, "closing")
            self.state.error(f"RISK close failed for {label}: {exc}")
            return None                              # still held; next poll tries again
        proceeds_usd = proceeds_lamports / LAMPORTS * sol_usd if sol_usd is not None else None
        cost = _f(pos.get("cost_usd"))
        pnl = round(proceeds_usd - cost, 2) if proceeds_usd is not None and cost is not None else None
        trade = {"order_id": pos.get("order_id"), "mode": pos["mode"], "ticker": pos.get("ticker"), "mint": pos["mint"],
                 "opened_at": pos.get("opened_at"), "closed_at": _now(), "cost_usd": cost,
                 "proceeds_usd": round(proceeds_usd, 2) if proceeds_usd is not None else None, "pnl_usd": pnl,
                 "proceeds_sol": proceeds_lamports / LAMPORTS,
                 "reason": why, "model": pos.get("model")}
        with self.state.lock:
            d = self.state.data
            d["trades"] = ([trade] + d["trades"])[:200]
            d["position"] = None
            if pnl is not None and d.get("day") == datetime.now(timezone.utc).date().isoformat():
                d["realized_today"] = (d.get("realized_today") or 0.0) + pnl
            self.state.save()
        wins = sum(1 for t in self.state.data["trades"] if (t.get("pnl_usd") or 0) > 0)
        losses = sum(1 for t in self.state.data["trades"] if (t.get("pnl_usd") or 0) < 0)
        self.state.agent("risk", f"closed {label}: {why}" + (f", ${pnl:+,.2f}" if pnl is not None else ", P&L unknown"),
                         pnl, f"{wins}W {losses}L")
        book.release()                               # the seat that closed the position frees the book
        self.data_misses = 0
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
            self.state.data["position"] = pos
            self.state.save()
        return pos

    def _reconcile(self, pos):
        amount, decimals = self.rpc.token_balance(self.kp.address, pos["mint"])
        with self.state.lock:
            if amount > 0:
                pos.update(status="open", tokens_raw=amount, decimals=decimals or pos.get("decimals"), cost_usd=None)
                self.state.data["position"] = pos
                self.state.agent("risk", f"{pos.get('ticker')}: swap landed after all, holding (entry cost unknown)", None, "holding")
            else:
                self.state.data["position"] = None
                self.state.agent("risk", f"{pos.get('ticker')}: swap never landed, desk released", None, "released")
                book.release()
            self.state.save()
        return None


def held_addr(held):
    r = book.DB.execute("SELECT addr FROM position WHERE id=1").fetchone()
    return r[0] if r else None
