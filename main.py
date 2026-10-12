# main.py
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

import book
import collect
from collect import (CHAIN_SET, DossierFailed, chain_state, dossier, market_state, shortlist, social_state,
                     trade_counts, universe)
from filter import chain_kill, free_kill, soft_kill, trade_kill
from pick import pick

CYCLE_SECONDS  = 900
GT_PER_MINUTE  = 10         # free tier (collect stays under it at 8/min)
UNIVERSE_PAGES = 2          # 3 chains x 2 pages = 6 GeckoTerminal slots. If 429s keep coming: 1
GT_DOSSIER     = 3          # what is left for dossiers in the same minute
DEX_BUDGET     = 25         # DexScreener calls per cycle, pass two only. If 429s keep coming: lower it
TICKET_SHARE   = 0.06       # the most SIZE could ever allow, as a share of free cash
JUDGE_LOG      = os.environ.get("JUDGE_LOG", "judge_log.jsonl")
NOT_THE_TOKENS_FAULT = {"dex_rate_limited", "trade_data_unavailable"}   # never benched
log = logging.getLogger("desk")


class StandDown(Exception):
    """The judge is unreachable or rejected the question: no guessing, the cycle stops here."""


def _log_answer(set_name, state, r, error=None):
    """Every answer, exactly as returned, with the model id. One line per call."""
    row = {"time": datetime.now(timezone.utc).isoformat(), "set": set_name, "state": state,
           "model": (r or {}).get("model"), "answers": (r or {}).get("answers"), "usage": (r or {}).get("usage"),
           "error": error}
    try:
        with open(JUDGE_LOG, "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError as exc:
        log.warning("could not write %s: %s", JUDGE_LOG, exc)


def _asker(judge):
    def ask(set_name, state):
        try:
            r = judge(set_name, state)
        except Exception as exc:
            status = getattr(exc, "status", None)
            _log_answer(set_name, state, None, error=str(exc)[:300])
            if status == 422:   # malformed question: every token hits the same wall. Never retried.
                raise StandDown(f"422 on {set_name}, cycle stopped: {exc}") from None
            raise StandDown(f"judge unavailable on {set_name}, desk stands down: {exc}") from None
        _log_answer(set_name, state, r)
        if not isinstance(r, dict) or not isinstance(r.get("answers"), dict):
            raise StandDown(f"judge answered {set_name} without answers")
        return r
    return ask


def _count(stats, stage, reason):
    stats[stage][reason] = stats[stage].get(reason, 0) + 1


def run_once(fomo, judge, desk, bank, shadow=True):
    if (h := book.held()):                       # RISK owns the desk right now: no scan at all
        log.info("holding %s for %.0f min, no scan this cycle", h["ticker"], h["minutes"])
        return None, {"held": h["ticker"], "minutes": round(h["minutes"])}

    stats = {"seen": 0, "benched": 0, "free": {}, "trade": {}, "chain": {}, "soft": {},
             "dossier_failed": 0, "stood_down": None, "no_order": None}
    if not (isinstance(bank, (int, float)) and not isinstance(bank, bool) and math.isfinite(bank) and bank > 0):
        stats["stood_down"] = f"no usable bank figure ({bank!r}): no ticket can be sized, no scan"
        return None, stats

    ask, survivors = _asker(judge), []
    gt_slots, dex_slots = GT_DOSSIER, DEX_BUDGET

    collect.UNIVERSE_FAILURES.clear()
    ids = universe(pages=UNIVERSE_PAGES)
    failures = list(collect.UNIVERSE_FAILURES)
    if failures:
        stats["universe_failed"] = failures
    if not ids:                                  # nothing to scan: say whether the list was empty or unreachable
        if failures:
            stats["stood_down"] = "token list unavailable: " + "; ".join(failures)[:300]
        else:
            stats["no_order"] = "no new pools listed"
        return None, stats
    try:
        listed = shortlist(fomo, ids)            # pass one: free, no per-token requests
    except Exception as exc:                     # the Privy bearer dies roughly hourly: refresh once
        log.info("FOMO list failed (%s), refreshing the bearer", exc)
        fomo.token()
        listed = shortlist(fomo, ids)

    try:
        for t in listed:
            stats["seen"] += 1
            if book.benched(t["tid"]):
                stats["benched"] += 1
                continue
            if (k := free_kill(t)):
                book.sit(t["tid"], k)
                _count(stats, "free", k)
                continue

            if dex_slots <= 0 or gt_slots <= 0:
                break                            # out of budget, not out of ideas

            t |= trade_counts(t)                 # pass two: one DexScreener call
            dex_slots -= 1
            if (k := trade_kill(t)):
                _count(stats, "trade", k)
                if k == "dex_rate_limited":
                    dex_slots = 0                # 429: no more DexScreener this cycle. Lower DEX_BUDGET.
                if k not in NOT_THE_TOKENS_FAULT:
                    book.sit(t["tid"], k)
                continue

            try:
                d = dossier(t)                   # pass three: one GeckoTerminal slot
            except DossierFailed as e:
                log.warning("dossier failed %s: %s", t["tid"], e)
                gt_slots -= 1                    # a failed call still cost you the slot
                stats["dossier_failed"] += 1
                book.sit(t["tid"], "dossier_failed")
                continue                         # missing is missing, not a pass
            gt_slots -= 1

            if (k := chain_kill(d)):
                book.sit(t["tid"], k)            # facts bench longest
                _count(stats, "chain", k)
                continue

            d["intended_ticket_usd"] = round(bank * TICKET_SHARE, 2)
            try:
                d["x_account"] = desk.read_x(d["x_handle"]) if d["x_handle"] else None
            except Exception as exc:             # missing is missing: no substitute account, smaller ticket
                log.warning("X read failed for %s: %s", d["x_handle"], exc)
                d["x_account"], d["x_account_status"] = None, "lookup failed"
            chain_set = CHAIN_SET[d["net"]]      # free_kill already refused anything unrouted

            ans, calls, asked = {}, [], ["market", chain_set]
            for set_name, state in (("market", market_state(d, d["intended_ticket_usd"])),
                                    (chain_set, chain_state(d, chain_set))):
                r = ask(set_name, state)         # pass four
                ans |= r["answers"]
                calls.append({"set": set_name, "model": r.get("model"), "usage": r.get("usage")})
            if d["x_account"]:
                r = ask("social", social_state(d))
                ans |= r["answers"]
                calls.append({"set": "social", "model": r.get("model"), "usage": r.get("usage")})
                asked.append("social")

            if (k := soft_kill(ans, d, asked)):
                book.sit(t["tid"], k)
                _count(stats, "soft", k)
                continue
            survivors.append((d, ans, calls))

        order, stats["no_order"] = pick(ask, survivors)   # pass five: one survivor or many, same path
    except StandDown as exc:
        log.warning("%s", exc)
        stats["stood_down"] = str(exc)
        return None, stats

    log.info("cycle: %(seen)s seen, %(benched)s benched, free %(free)s, trade %(trade)s, chain %(chain)s, "
             "soft %(soft)s, no order: %(no_order)s", stats)
    if order is None:
        return None, stats                       # no order is also an order: report() says why
    if shadow:
        desk.log_shadow(order, stats)            # written, never sent
        return None, stats

    book.take(order)          # the desk is now held. No scan until RISK calls release().
    return order, stats


def main(fomo, judge, desk, shadow=True):
    """desk is your Grok Bot side. It has to provide five things:
         bank()                 -> float, free cash right now
         read_x(handle)         -> the X block SOCIAL collects with its plugin, or None
         log_shadow(order, st)  -> append a row for the shadow week
         report(order, stats)   -> one line to Telegram, trade or no trade (stats["no_order"] /
                                   stats["stood_down"] say why there was none)
         send_to_seats(order)   -> hand it to SIZE, then FILLS, then RISK, in that order
    judge(set, state) -> the desk's raw response; judge_client.judge does the HTTP and the retry rules.
    """
    while True:
        try:
            fomo.token()                         # Privy bearer lives ~60 min, refresh it
            order, stats = run_once(fomo, judge, desk, desk.bank(), shadow)
            desk.report(order, stats)            # every cycle, trade or not
            if order:
                desk.send_to_seats(order)
        except Exception as e:
            log.exception("cycle blew up: %s", e)
        time.sleep(CYCLE_SECONDS)
