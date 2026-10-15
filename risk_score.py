"""Research-only token risk scoring and user-rule checks. No trade execution."""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class ScoreResult:
    score: int
    verdict: str
    red_flags: tuple[str, ...]
    green_flags: tuple[str, ...]
    unknowns: tuple[str, ...]

def score_token(*, top10_pct, dev_pct, block0_buyers,
                identical_block0_amounts=None, x_account_age_days=None,
                x_followers=None, curve_progress_pct=None, steady_buys=None,
                dev_has_sold=None, token_age_hours=None, new_ath_within_6h=None,
                dev_sold_within_10m=None):
    score=5
    red, green, unknown=[], [], []
    if block0_buyers is None: unknown.append("block-0 buyer count unavailable")
    elif block0_buyers > 3:
        score-=1; red.append(f"{block0_buyers} block-0 buyers (rule: max 3)")
    else: green.append(f"{block0_buyers} block-0 buyers (within rule)")
    if identical_block0_amounts is None: unknown.append("identical block-0 buy amounts not checked")
    elif identical_block0_amounts:
        score-=1; red.append("identical block-0 buy amounts suggest a possible bundle")
    else: green.append("no identical-amount bundle pattern detected")
    if top10_pct is None: unknown.append("top-10 holder concentration unavailable; curve/LP exclusion must be verified")
    elif top10_pct > 25:
        score-=1; red.append(f"top 10 holders own {top10_pct:.1f}% excluding curve/LP (rule: max 25%)")
    else: green.append(f"top 10 holders own {top10_pct:.1f}% excluding curve/LP (within rule)")
    if x_account_age_days is None or x_followers is None: unknown.append("X account age/follower data unavailable")
    elif x_account_age_days < 7 and x_followers < 100:
        score-=1; red.append(f"X account is {x_account_age_days} days old with {x_followers} followers")
    else: green.append(f"X account age/followers: {x_account_age_days} days / {x_followers}")
    if curve_progress_pct is None: unknown.append("bonding-curve progress unavailable")
    elif 15 <= curve_progress_pct <= 80:
        score+=1; green.append(f"curve at {curve_progress_pct:.1f}% (inside 15–80% research range)")
    else:
        score-=1; red.append(f"curve at {curve_progress_pct:.1f}% (outside 15–80% research range)")
    if steady_buys is None: unknown.append("buy-flow steadiness not checked")
    elif steady_buys: score+=1; green.append("buy flow appears steady")
    else: score-=1; red.append("buy flow is not steady")
    if dev_pct is None: unknown.append("developer holding percentage unavailable")
    elif dev_pct > 5:
        score-=1; red.append(f"developer holds {dev_pct:.1f}% (rule: max 5%)")
    else: green.append(f"developer holds {dev_pct:.1f}% (within rule)")
    if dev_has_sold is None: unknown.append("developer sell history unavailable")
    elif dev_has_sold:
        score-=1; red.append("developer sells detected")
    else: green.append("no developer sells detected in available history")
    if dev_sold_within_10m is None: unknown.append("developer first-10-minute sell check unavailable")
    elif dev_sold_within_10m:
        score-=2; red.append("developer sold within first 10 minutes (hard-rule failure)")
    else: green.append("no developer sale detected in first 10 minutes")
    if token_age_hours is None: unknown.append("token age unavailable")
    elif token_age_hours > 6:
        if new_ath_within_6h is None: unknown.append("new-ATH history for older token unavailable")
        elif not new_ath_within_6h:
            score-=2; red.append(f"token is {token_age_hours:.1f} hours old with no new ATH (hard-rule failure)")
        else: green.append("older token has a new ATH in the checked period")
    else: green.append(f"token age {token_age_hours:.1f} hours (within 6-hour age rule)")
    score=max(0,min(10,score))
    hard_fail=((top10_pct is not None and top10_pct>25)
        or (dev_pct is not None and dev_pct>5)
        or (block0_buyers is not None and block0_buyers>3)
        or (curve_progress_pct is not None and not 15<=curve_progress_pct<=80)
        or identical_block0_amounts is True or dev_sold_within_10m is True
        or (token_age_hours is not None and token_age_hours>6 and new_ath_within_6h is False))
    if hard_fail or score<5: verdict="SKIP"
    elif unknown: verdict="REVIEW — DATA INCOMPLETE"
    else: verdict="REVIEW — NOT A BUY SIGNAL"
    return ScoreResult(score,verdict,tuple(red),tuple(green),tuple(unknown))

def trade_window_status(now=None):
    """Quebec local-time quiet period: 01:00–08:59."""
    tz=ZoneInfo("America/Toronto")
    local_now=(now or datetime.now(tz)).astimezone(tz)
    if 1 <= local_now.hour < 9:
        return False, f"Quiet hours in Quebec: {local_now:%H:%M}; allowed window starts at 09:00."
    return True, f"Outside quiet hours in Quebec: {local_now:%H:%M}. Schedule check only."

def format_token_report(result):
    red=list(result.red_flags)+[f"UNVERIFIED: {x}" for x in result.unknowns]
    lines=[f"SCORE: {result.score}/10","RED FLAGS:"]
    lines += [f"- {x}" for x in red] or ["- None detected from available data"]
    lines.append("GREEN FLAGS:")
    lines += [f"- {x}" for x in result.green_flags] or ["- None verified"]
    lines.append(f"VERDICT: {result.verdict}")
    return "\n".join(lines)
