# pick.py
"""Pass five: one call to the "pick" set. worth_trading_at_all always ships; "best" ships only with two
or more candidates (a choice over one option returns that option and proves nothing).

Candidates are labelled c1, c2, ... in code. Never by ticker: tickers are written by whoever launched the
token, two can be the same (the second would silently replace the first and could be bought instead), and a
label is shown to Jev as an option, so a ticker label is a prompt-injection channel.
Each summary is two lines assembled in code: line 1 the dossier's facts, line 2 Jev's own answers.
"""
import math
from datetime import datetime, timezone

from filter import evidence_gaps
from thresholds import DARK_TICKET_CUT, NO_FAVOURITE_CUT, NO_SOCIAL_CUT, PICK_MIN_CONF, PICK_MIN_WORTH


def _f(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _fmt(v, spec, unknown="unknown"):
    v = _f(v)
    return unknown if v is None else format(v, spec)


def summary(d, ans) -> str:
    """Two lines per candidate, built from facts and answers Jev already gave. Never the raw dossier."""
    holders = d.get("holder_count")
    line1 = (f"{d.get('chain')}, {_fmt(d.get('age_minutes'), '.0f')}m old, ${_fmt(d.get('mcap_usd'), ',.0f')} mcap, "
             f"${_fmt(d.get('liquidity_usd'), ',.0f')} liq, {holders if holders is not None else 'unknown'} holders")
    bits = [f"crowd {_fmt((ans.get('shape', {}).get('probabilities') or {}).get('crowd'), '.2f')}"]
    gaps = set(evidence_gaps(ans, d))
    conc = ans.get("concentration_is_exit_risk") or {}
    bits.append("concentration risk " + ("no evidence" if "concentration_is_exit_risk" in gaps
                                         else _fmt(conc.get("noul"), ".2f")))
    for key, label in (("authority_risk", "authority"), ("sell_side_risk", "sell side"), ("data_coverage", "data")):
        if key in ans:
            bits.append(f"{label} {ans[key].get('choice') or 'unknown'}")
    if "account_is_the_project" in ans:
        bits.append(f"official account {_fmt(ans['account_is_the_project'].get('noul'), '.2f')}, "
                    f"effort {_fmt((ans.get('effort') or {}).get('score'), '.1f')}")
    else:
        bits.append("no usable X account")
    return line1 + "\n" + "; ".join(bits)


def size_factor(d, ans) -> tuple[float, list[str]]:
    """The only size input the pick hands on. Same function for one survivor or many.
    Less visibility (Robinhood dark, or a 'cut' question with no evidence) cuts once; no X account cuts;
    the two stack."""
    factor, why = 1.0, []
    dark = (ans.get("data_coverage") or {}).get("choice") == "dark"
    gaps = evidence_gaps(ans, d)
    if dark or gaps:
        factor *= DARK_TICKET_CUT
        why.append("data dark" if dark else "no evidence: " + ", ".join(gaps))
    if "account_is_the_project" not in ans:
        factor *= NO_SOCIAL_CUT
        why.append("no usable X account")
    return round(factor, 4), why


def pick(judge, survivors):
    """survivors: [(dossier, answers, calls), ...] where calls = [{"set", "model", "usage"}, ...].
    Returns (order, None) or (None, reason). Judge errors are raised: main stands the desk down."""
    if not survivors:
        return None, "no survivors"
    labels = {f"c{i + 1}": s for i, s in enumerate(survivors)}
    state = {"candidates": {label: summary(s[0], s[1]) for label, s in labels.items()}}
    r = judge("pick", state)
    answers = r.get("answers") or {}

    worth = _f((answers.get("worth_trading_at_all") or {}).get("noul"))
    if worth is None:
        return None, "worth_trading_at_all came back without a value"
    if worth < PICK_MIN_WORTH:
        return None, f"worth_trading_at_all {worth:.2f} < {PICK_MIN_WORTH}: not a day"

    unsure = None
    if len(labels) == 1:
        label, confidence, runner_up = "c1", None, []
    else:
        best = answers.get("best") or {}
        label, confidence = best.get("choice"), _f(best.get("confidence"))
        if confidence is None:
            return None, "best came back without a confidence"
        if confidence < PICK_MIN_CONF:
            # Every candidate here passed every check. Jev not having a clear favourite between good coins is not a
            # reason to buy none of them (that left the desk buying nothing all day): its top choice, half the ticket.
            unsure = f"no clear favourite (confidence {confidence:.2f} < {PICK_MIN_CONF})"
        if label not in labels:
            return None, f"best chose {label!r}, which is not a candidate"
        probs = best.get("probabilities") if isinstance(best.get("probabilities"), dict) else {}
        ranked = sorted(((k, p) for k, p in probs.items() if k in labels and k != label and _f(p) is not None),
                        key=lambda kv: -kv[1])
        runner_up = [{"ticker": labels[k][0].get("ticker"), "address": labels[k][0].get("addr"), "p": p}
                     for k, p in ranked[:1]]

    d, ans, calls = labels[label]
    factor, cuts = size_factor(d, ans)
    if unsure:
        factor = round(factor * NO_FAVOURITE_CUT, 4)
        cuts = cuts + [unsure]
    return {
        "order_id": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": r.get("model"),
        "token": {"ticker": d.get("ticker"), "address": d["addr"], "network_id": d["net"], "chain": d["chain"]},
        "size_factor": factor,
        "size_cuts": cuts,
        "confidence": confidence,                    # None for a single survivor: no choice was run
        "worth_trading_at_all": worth,
        "runner_up": runner_up,
        "facts": {k: d.get(k) for k in ("liquidity_usd", "price_usd", "mcap_usd", "decimals", "age_minutes")},
        "why": ans,                                  # for the log and for you, never an input to a seat
        "judge_calls": calls + [{"set": "pick", "model": r.get("model"), "usage": r.get("usage")}],
    }, None
