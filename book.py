# book.py
import os
import sqlite3
import time

DB = sqlite3.connect(os.environ.get("DESK_DB", "desk.db"), check_same_thread=False)
DB.executescript("""
CREATE TABLE IF NOT EXISTS position(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  ticker TEXT, addr TEXT, net INT, opened_at REAL);
CREATE TABLE IF NOT EXISTS bench(
  tid TEXT PRIMARY KEY, reason TEXT, until REAL);
""")

# how long a rejection stands, by what fired it
BENCH_MINUTES = {
    # facts that will not change while this token exists
    "honeypot": 100_000, "authority_open": 100_000, "token_program_can_block": 100_000,
    "top_wallet": 100_000, "sell_side": 100_000, "no_chain_set": 100_000,
    # slow to change
    "recycled_account": 360, "account_is_the_project": 360,
    # can change as the float moves
    "top_10": 90, "holders": 90, "dev_still_loaded": 90,
    "concentration_is_exit_risk": 90,
    # can change inside the hour, keep it short or you miss the token maturing
    "shape": 25, "shape_weak": 25, "momentum_already_spent": 25,
    "liquidity_fits_ticket": 25, "liquidity": 25, "volume": 25,
    "trades": 25, "mcap": 25, "age": 20, "no_sells": 25, "no_pair": 25,
    # unknown, not bad: the data may simply not be there yet. Short, so the token gets another look.
    "age_unknown": 20, "liquidity_unknown": 20, "volume_unknown": 20, "mcap_unknown": 20,
    "trades_unknown": 20, "trades_h1_unknown": 20, "authority_unknown": 20, "top_wallet_unknown": 20,
    "sell_side_unknown": 25, "data_coverage_missing": 20,
    "sellable_by_evidence_no_evidence": 25, "liquidity_fits_ticket_no_evidence": 25,
    # the dossier call failed (rate limit, network): not the token's fault, but it cost a slot
    "dossier_failed": 20,
    # held right now: look again soon, RISK may have sold it
    "already_held": 5,
    # just sold: not straight back into the coin RISK got out of. A coin that lost money is left alone for a day
    # (HIRE and SEAN were bought back and lost again); a winner for three hours, so it can't be chased at the top
    "sold": 180, "sold_loss": 1440,
    # a desk pick the sniper's checks turned down
    "sniper_gate": 60,
}
DEFAULT_BENCH = 45      # anything not listed, e.g. "<question>_missing"


def held():
    r = DB.execute("SELECT ticker, opened_at FROM position WHERE id=1").fetchone()
    return {"ticker": r[0], "minutes": (time.time() - r[1]) / 60} if r else None


def take(order):
    t = order["token"]
    DB.execute("INSERT OR REPLACE INTO position VALUES (1,?,?,?,?)",
               (t["ticker"], t["address"], t["network_id"], time.time()))
    DB.commit()


def release():
    """RISK calls this the moment a close is filled. Nothing else calls it."""
    DB.execute("DELETE FROM position")
    DB.commit()


def benched(tid: str) -> bool:
    r = DB.execute("SELECT until FROM bench WHERE tid=?", (tid,)).fetchone()
    return bool(r and r[0] > time.time())


def bench_counts(top: int = 10):
    """The desk's rejections still standing, by reason: [{"reason": ..., "coins": n}, ...], biggest first."""
    rows = DB.execute("SELECT reason, COUNT(*) FROM bench WHERE until > ? GROUP BY reason ORDER BY 2 DESC LIMIT ?",
                      (time.time(), top)).fetchall()
    return [{"reason": r, "coins": n} for r, n in rows]


def sit(tid: str, reason: str):
    mins = BENCH_MINUTES.get(reason, DEFAULT_BENCH)
    DB.execute("INSERT OR REPLACE INTO bench VALUES (?,?,?)",
               (tid, reason, time.time() + mins * 60))
    DB.commit()
