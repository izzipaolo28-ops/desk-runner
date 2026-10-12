# questions.py  (lives on the desk, next to judge.py)
"""Question sets the desk serves. Bots send a set name + state; the questions live here only.

Routing is in code (collect.CHAIN_SET): each chain gets the set written for its own evidence.
mint/freeze authority are real facts on Solana and do not exist on EVM chains, so only the Solana set reads
them, as booleans the chain itself reported (*_revoked: true / false / null = could not be read).
Every question that reads a field that can be null says so: unknown is never safe and never neutral.
"""
import re

from typesafe_sdk import Choice, Noul, Score

NULL_RULE = " A null value means that fact is unknown. Unknown is never safe and never neutral."


def _n(text):
    return text + NULL_RULE


MARKET = {
    "shape": Choice(
        instructions=_n("Classify the shape of this launch from the fields in `state`."),
        criteria={
            "crowd": "Holders growing faster than price. Buys outnumber sells across both "
                     "recent windows. Volume spread rather than spiking once.",
            "one_buyer": "Price climbing faster than holders. Holder growth flat while "
                         "price rises. One wallet walking the price up.",
            "fading": "Recent volume is a small fraction of the daily average, or sells "
                      "outnumber buys in both recent windows.",
            "too_early": "Too few data points to tell any of the above apart yet.",
        }),
    "liquidity_fits_ticket": Noul(
        instructions=_n("A position of `intended_ticket_usd` could be exited into "
                        "`liquidity_usd` without moving the price more than a few percent.")),
    "momentum_already_spent": Noul(
        instructions=_n("The move in `change` has already happened, so entering now means "
                        "buying after the information is public."),
        criteria={"true": "The largest change sits in the older windows.",
                  "false": "The recent windows carry the move."}),
}

CONCENTRATION = Noul(
    instructions=_n("Holding this token means being exit liquidity, based on "
                    "`top_10_percent`, `top_wallet_percent` (both in percent; the top wallet excludes pool "
                    "and other program accounts) and `holder_count`."),
    criteria={"true": "A few wallets can end the market by selling.",
              "false": "The float is spread widely enough to absorb a large holder."})

DEV_LOADED = Noul(
    instructions=_n("`developer_holding_percentage` is large enough that the creator "
                    "selling would meaningfully move the price."))

CHAIN_SOLANA = {                      # 1399811149
    "authority_risk": Choice(
        instructions="Judge contract control risk from `mint_authority_revoked` and `freeze_authority_revoked` "
                     "(true = revoked, false = still set, null = could not be read).",
        criteria={
            "renounced": "Both true. Supply cannot be inflated, balances cannot be frozen.",
            "mint_open": "mint_authority_revoked is false. Supply can be inflated at will.",
            "freeze_open": "freeze_authority_revoked is false. Balances can be frozen at will.",
            "both_open": "Both are false.",
            "unknown": "Either one is null, so control cannot be judged.",
        }),
    "concentration_is_exit_risk": CONCENTRATION,
    "dev_still_loaded": DEV_LOADED,
}

CHAIN_BSC = {                         # 56, and Base (8453) by routing
    "sell_side_risk": Choice(
        instructions=_n("Judge whether a position here can be sold, from `is_honeypot`, "
                        "`gt_score_details` and the buy and sell counts."),
        criteria={
            "clean": "Not flagged, and sells are going through in the data.",
            "flagged": "Explicitly flagged as a honeypot.",
            "suspicious": "Not flagged, but sells are absent or vanishingly rare while "
                          "buys are plentiful.",
            "unknown": "The honeypot field is missing or unknown and trade counts are "
                       "too thin to stand in for it.",
        }),
    "concentration_is_exit_risk": CONCENTRATION,
    "pool_quality": Score(
        instructions=_n("Rate the pool from `gt_score_details` and `liquidity_usd`."),
        criteria=["Thin and new. One withdrawal ends the market.",
                  "Usable, but a large ticket would move it.",
                  "Deep enough that normal desk size is invisible."]),
}

CHAIN_ROBINHOOD = {                   # 4663, the one with holes in the data
    "data_coverage": Choice(
        instructions="Judge how much of this token is visible, from which fields in "
                     "`state` carry values and which are null or unknown.",
        criteria={
            "indexed": "Holder count and distribution present, honeypot field is a real "
                       "answer.",
            "partial": "Holder count present from the venue, but distribution or the "
                       "honeypot field is missing.",
            "dark": "Neither distribution nor honeypot available. Only price, volume and "
                    "pool age are known.",
        }),
    "sellable_by_evidence": Noul(
        instructions=_n("Sells are going through on this token, judged from the buy and sell "
                        "counts rather than from any honeypot flag."),
        criteria={"true": "Sells appear across recent windows in a normal ratio.",
                  "false": "Buys with almost no sells, or no trades at all."}),
    "concentration_is_exit_risk": CONCENTRATION,
    "dev_still_loaded": DEV_LOADED,
}

SOCIAL = {
    "account_is_the_project": Noul(
        instructions=_n("The account in `x_account` is the token's official account, not a "
                        "fan account, an impersonator, or an unrelated similar name."),
        criteria={"true": "Its handle matches `handle_published_by_token` and the content is "
                          "about this token.",
                  "false": "Similar name, different subject, or no link back."}),
    "audience_is_real": Noul(
        instructions=_n("Engagement in `x_account` is consistent with its follower count, "
                        "rather than a large follower number with almost no replies or "
                        "reposts on recent posts.")),
    "recycled_account": Noul(
        instructions=_n("`x_account` shows signs of being repurposed: far older than the "
                        "token, with a handle or content history belonging to a different "
                        "project.")),
    "effort": Score(
        instructions=_n("Rate how much work is visibly behind this project from `x_account`."),
        criteria=["One post, one image, nothing else.",
                  "A handful of posts, all promotional.",
                  "Regular posting with substance beyond price.",
                  "A visible team shipping visible things."]),
}

_LABEL = re.compile(r"^c[1-9][0-9]{0,2}$")


def PICK(state):
    """Options are built at call time from state["candidates"]: {"c1": "<two lines>", ...}.
    Labels are made in code (never tickers). Each summary appears once, in the state; the options point at it.
    worth_trading_at_all always ships. "best" ships only with two or more candidates: a choice over one
    option returns that option and proves nothing."""
    cands = state.get("candidates") if isinstance(state, dict) else None
    if not isinstance(cands, dict) or not cands:
        raise ValueError("pick needs state.candidates: an object of label -> two-line summary")
    if len(cands) > 255:
        raise ValueError("pick allows at most 255 candidates")
    for label, text in cands.items():
        if not isinstance(label, str) or not _LABEL.match(label):
            raise ValueError("candidate labels are c1, c2, ... (made in code, never a ticker)")
        if not isinstance(text, str) or text.count("\n") != 1:
            raise ValueError(f"candidate {label}: the summary must be exactly two lines")
    questions = {
        "worth_trading_at_all": Noul(
            instructions="At least one token in `candidates` is worth a position today, "
                         "rather than all of them being mediocre.",
            criteria={"true": "At least one is a clean setup.",
                      "false": "Every candidate has a disqualifying weakness."}),
    }
    if len(cands) >= 2:
        questions["best"] = Choice(
            instructions="Choose the single token in `candidates` that is the best entry "
                         "right now. Weigh crowd shape, contract risk, concentration and "
                         "the project account together. Prefer a clean unspent setup over "
                         "a larger move that already happened.",
            criteria={label: f"The token described in candidates.{label}" for label in cands})
    return questions


# ---- the memecoin-monitor bot's sets (its own pick is "funnel_pick", so the two picks never collide) ----
RISK_LEVELS = [
    "1/10: no rug-pull or scam signals at all",
    "2/10: negligible risk",
    "3/10: low risk",
    "4/10: low to moderate risk",
    "5/10: moderate risk",
    "6/10: elevated risk",
    "7/10: high risk",
    "8/10: very high risk",
    "9/10: severe risk, likely a scam",
    "10/10: extreme, almost certainly a rug pull or scam",
]
TOKEN = {
    "action": Choice(
        instructions=_n("Should I enter a position in this token right now?"),
        criteria={
            "buy": "Strong momentum, healthy buy pressure, acceptable risk",
            "hold": "Not enough signal to act either way",
            "skip": "Red flags outweigh any opportunity here",
            "missing_data": "Facts this decision depends on are null (unknown), so it cannot be judged",
        }),
    "risk_score": Score(
        instructions=_n("How likely is this newly launched token to be a rug pull or scam?"),
        criteria=RISK_LEVELS),
}
FUNNEL_PICK_INSTRUCTIONS = _n("Pick the ONE candidate that is the best trade right now. Choose none unless one is "
                              "clearly good on market, chain and social evidence together.")
_FUNNEL_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def FUNNEL_PICK(state):
    cands = state.get("candidates") if isinstance(state, dict) else None
    if not isinstance(cands, dict) or not cands:
        raise ValueError("funnel_pick needs state.candidates: a non-empty object")
    if len(cands) > 254:
        raise ValueError("funnel_pick allows at most 254 candidates")
    for label in cands:
        if not isinstance(label, str) or not _FUNNEL_LABEL.match(label) or label == "none":
            raise ValueError("candidate labels must be 1-40 letters/digits/_/- and not 'none'")
    criteria = {label: f"Candidate {label}: see state.candidates.{label}" for label in cands}
    criteria["none"] = "None of the candidates is a good enough trade right now"
    return {"pick": Choice(instructions=FUNNEL_PICK_INSTRUCTIONS, criteria=criteria)}


SETS = {"market": MARKET, "social": SOCIAL, "pick": PICK,
        "solana": CHAIN_SOLANA, "bsc": CHAIN_BSC, "robinhood": CHAIN_ROBINHOOD,
        "token": TOKEN, "funnel_pick": FUNNEL_PICK}
