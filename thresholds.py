# thresholds.py
# Every number that decides anything lives here. If you disagree with a pick, you disagree with a line below.

HARD = {                        # stage 2, arithmetic, runs before anything costs money
    "min_age_minutes":   60,    # the desk's coins are an hour old or more: younger ones are the sniper's
    "max_age_hours":     72,    # older than this and it is not a launch any more
    "min_liquidity_usd": 12_000,
    "min_volume_h24":    40_000,
    "min_mcap_usd":      60_000,
    "max_mcap_usd":      8_000_000,
    "min_trades_h24":    150,
    "max_top_wallet":    0.05,  # solana only, exact, from RPC: the largest KEY-HOLDING wallet (pool vaults,
                                # bonding curves and other program accounts excluded; they are not holders)
    "max_top_10":        0.60,  # where distribution exists
    "min_holders":       80,
}

SOFT = {                        # applied to Jev's answers, per token
    "concentration_is_exit_risk": ("max", 0.55),
    "momentum_already_spent":     ("max", 0.60),
    "liquidity_fits_ticket":      ("min", 0.60),
    "account_is_the_project":     ("min", 0.70),
    "recycled_account":           ("max", 0.50),
    "audience_is_real":           ("min", 0.45),
    "effort":                     ("min", 1.0),    # 0-based level: 1.0 = "a handful of posts"
    "dev_still_loaded":           ("max", 0.55),
    "sellable_by_evidence":       ("min", 0.60),   # robinhood
}

# A question whose every input came back null was answered from nothing, and that number is not
# evidence. It is never a pass. Its own consequence:
#   "cut"  -> keep the token, but the ticket is cut once by DARK_TICKET_CUT (less visibility, smaller ticket;
#             the same cut as data_coverage == dark, applied once, stacking with NO_SOCIAL_CUT)
#   "kill" -> bench the token under "<question>_no_evidence"
NO_EVIDENCE = {
    "concentration_is_exit_risk": "cut",
    "dev_still_loaded":           "cut",
    "momentum_already_spent":     "cut",
    "liquidity_fits_ticket":      "kill",
    "sellable_by_evidence":       "kill",   # sellability is safety: no trades seen, no position
}

SHAPE_MIN_CROWD = 0.55          # probabilities["crowd"], not the winning label
PICK_MIN_WORTH  = 0.60
PICK_MIN_CONF   = 0.55
DARK_TICKET_CUT = 0.40          # robinhood data_coverage == dark, or any "cut" question above with no evidence
NO_SOCIAL_CUT   = 0.60          # no usable X handle: trade smaller, do not skip
NO_FAVOURITE_CUT = 0.50         # several coins passed everything and Jev has no clear favourite: its top choice, half the ticket
