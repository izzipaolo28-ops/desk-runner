# filter.py
"""The cuts. Facts first and free, Jev's answers last.

Null never means fine. Every check that can't see its input fails with its own reason ("liquidity_unknown",
not "liquidity"), so book.py can bench it for its own length and the log says what was missing.
free_kill and trade_kill stamp the token (t["stage2"], t["stage3"]); collect.trade_counts and
collect.dossier refuse a token without the stamp, so a dead token never spends another request.
"""
import math

from collect import CHAIN_SET
from thresholds import HARD, NO_EVIDENCE, SHAPE_MIN_CROWD, SOFT

# Questions each set asks (must match the desk's questions.py; a test checks it). An asked question that
# comes back without an answer is "<name>_missing", never a pass.
ASKED = {
    "market":    ("shape", "liquidity_fits_ticket", "momentum_already_spent"),
    "solana":    ("authority_risk", "concentration_is_exit_risk", "dev_still_loaded"),
    "bsc":       ("sell_side_risk", "concentration_is_exit_risk", "pool_quality"),
    "robinhood": ("data_coverage", "sellable_by_evidence", "concentration_is_exit_risk", "dev_still_loaded"),
    "social":    ("account_is_the_project", "audience_is_real", "recycled_account", "effort"),
}
# The dossier fields each soft question reads. All null -> the answer is not evidence (see NO_EVIDENCE).
INPUTS = {
    "concentration_is_exit_risk": ("top_10_percent", "top_wallet_percent", "holder_count"),
    "dev_still_loaded":           ("developer_holding_percentage",),
    "momentum_already_spent":     ("change",),
    "liquidity_fits_ticket":      ("liquidity_usd", "intended_ticket_usd"),
    "sellable_by_evidence":       ("buys_h1", "sells_h1", "buys_h6", "sells_h6"),
}
NEEDS_ALL = {"liquidity_fits_ticket"}   # a fit needs both the pool and the ticket


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _stamp(t, stage, reason):
    t[stage] = "pass" if reason is None else f"kill: {reason}"
    return reason


def free_kill(t) -> str | None:
    """Pass one. Runs on the whole universe, costs nothing, touches no network.
    Everything it reads came back with the FOMO batch."""
    return _stamp(t, "stage2", _free_reason(t))


def _free_reason(t):
    if CHAIN_SET.get(t.get("net")) is None:
        return "no_chain_set"                        # nothing to ask for it, so never pay for it
    age, liq, vol, mcap = t.get("age_minutes"), t.get("liquidity_usd"), t.get("volume_h24"), t.get("mcap_usd")
    if age is None:
        return "age_unknown"
    if not HARD["min_age_minutes"] <= age <= HARD["max_age_hours"] * 60:
        return "age"
    if liq is None:
        return "liquidity_unknown"
    if liq < HARD["min_liquidity_usd"]:
        return "liquidity"
    if vol is None:
        return "volume_unknown"
    if vol < HARD["min_volume_h24"]:
        return "volume"
    if mcap is None:
        return "mcap_unknown"
    if not HARD["min_mcap_usd"] <= mcap <= HARD["max_mcap_usd"]:
        return "mcap"
    return None


def trade_kill(t) -> str | None:
    """Pass two. One DexScreener call already spent on this token. Tens, not hundreds.
    dex_rate_limited / trade_data_unavailable are not the token's fault: main does not bench them."""
    return _stamp(t, "stage3", _trade_reason(t))


def _trade_reason(t):
    status = t.get("trade_data")
    if status == "rate_limited":
        return "dex_rate_limited"
    if status == "unavailable":
        return "trade_data_unavailable"
    if status != "ok":                               # "no pair", "chain not on DexScreener"
        return "no_pair"
    if t.get("trades_h24") is None:
        return "trades_unknown"
    if t["trades_h24"] < HARD["min_trades_h24"]:
        return "trades"
    buys, sells = t.get("buys_h1"), t.get("sells_h1")
    if buys is None or sells is None:
        return "trades_h1_unknown"
    if sells == 0 and buys > 20:
        return "no_sells"
    return None


def chain_kill(d) -> str | None:
    """After the dossier, still free. Facts, not judgements. Facts that never change go first, so a token
    that fails several is benched for the longest of them and never re-paid for."""
    chain_set = CHAIN_SET.get(d.get("net"))
    if d.get("is_honeypot") is True:
        return "honeypot"                            # any chain that reports it; a fact
    if chain_set == "solana":
        mint, freeze = d.get("mint_authority_revoked"), d.get("freeze_authority_revoked")
        if mint is False or freeze is False:
            return "authority_open"
        if d.get("sell_block_possible") is True:
            return "token_program_can_block"         # Token-2022 hook, fee, delegate, freeze-by-default
        if mint is None or freeze is None:
            return "authority_unknown"
        top = d.get("top_wallet_percent")
        if top is not None:
            if top / 100 > HARD["max_top_wallet"]:
                return "top_wallet"
        elif not (d.get("top_wallet_at_most_percent") is not None
                  and d["top_wallet_at_most_percent"] / 100 <= HARD["max_top_wallet"]):
            return "top_wallet_unknown"
    if d.get("top_10_percent") is not None and d["top_10_percent"] / 100 > HARD["max_top_10"]:
        return "top_10"
    if d.get("holder_count") is not None and d["holder_count"] < HARD["min_holders"]:
        return "holders"
    return None                                      # unknown distribution/holders: Jev sees the nulls


def _present(v):
    if isinstance(v, dict):
        return any(x is not None for x in v.values())
    return v is not None


def has_evidence(d, name) -> bool:
    if d is None or name not in INPUTS:
        return True
    seen = [_present(d.get(f)) for f in INPUTS[name]]
    return all(seen) if name in NEEDS_ALL else any(seen)


def evidence_gaps(ans, d) -> list[str]:
    """Questions answered from no evidence whose consequence is a ticket cut (pick.size_factor reads this)."""
    return [n for n, rule in NO_EVIDENCE.items() if rule == "cut" and n in ans and not has_evidence(d, n)]


def soft_kill(ans, d=None, asked=()) -> str | None:
    """Jev's answers against SOFT. First failure wins. `asked` = the sets called for this token;
    `d` = the dossier the states were built from (to tell an answer from a number made of nothing)."""
    for set_name in asked:
        for q in ASKED.get(set_name, ()):
            if not isinstance(ans.get(q), dict):
                return f"{q}_missing"

    for name, (direction, limit) in SOFT.items():
        a = ans.get(name)
        if a is None:
            continue                                 # not asked for this chain (routing), or no X account
        if not has_evidence(d, name):
            if NO_EVIDENCE.get(name) == "kill":
                return f"{name}_no_evidence"
            continue                                 # "cut": not a pass, not a fail; pick cuts the ticket
        v = a.get("noul", a.get("score"))
        if not _finite(v):
            return f"{name}_missing"
        if direction == "max" and v > limit:
            return name
        if direction == "min" and v < limit:
            return name

    shape = ans.get("shape")
    if shape is not None:
        crowd = (shape.get("probabilities") or {}).get("crowd")
        if shape.get("choice") is None or not _finite(crowd):
            return "shape_missing"
        if shape["choice"] in ("fading", "one_buyer"):
            return "shape"
        if crowd < SHAPE_MIN_CROWD:
            return "shape_weak"

    side = ans.get("sell_side_risk")
    if side is not None:
        if side.get("choice") in ("flagged", "suspicious"):
            return "sell_side"
        if side.get("choice") != "clean":
            return "sell_side_unknown"               # its own option, its own consequence: no position

    auth = ans.get("authority_risk")
    if auth is not None:
        if auth.get("choice") == "unknown" or auth.get("choice") is None:
            return "authority_unknown"
        if auth["choice"] != "renounced":
            return "authority_open"

    cov = ans.get("data_coverage")
    if cov is not None and cov.get("choice") not in ("indexed", "partial", "dark"):
        return "data_coverage_missing"               # dark is not a kill: pick cuts the ticket
    return None
