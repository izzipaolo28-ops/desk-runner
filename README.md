# Desk Runner

The desk, the bot and the DESK FLOOR dashboard in one Railway service. It scans Solana tokens, asks Jev through your
question sets, buys the pick through Jupiter, then RISK sells it when the volume dies. Up to 5 coins at once (`MAX_POSITIONS`).

It starts in **paper mode**: real Jupiter quotes, nothing sent, no wallet needed. Real money only after you set
`LIVE_MODE=true` and give it a bot wallet.

Everything below works from an iPhone in Safari.

---

## 1. Put the files on GitHub (5 min)

1. In the **Files** app, tap `desk-runner.zip`. It unzips into a `desk-runner` folder.
2. In Safari, go to **github.com/new**. Name it `desk-runner`, choose **Private**, tap **Create repository**.
3. On the empty repo page, tap **uploading an existing file**, then **choose your files**.
4. Open the `desk-runner` folder, tap **Select**, and tap every **file**. Skip the `tests` folder: the desk doesn't
   need it to run. Tap **Open**, then **Commit changes**.
5. Check: the repo's front page lists `runner.py`, `railway.json` and `requirements.txt` directly, not inside a
   folder.

## 2. Deploy on Railway (5 min)

1. Go to **railway.com**, tap **Login**, and choose GitHub.
2. Tap **New Project**, then **Deploy from GitHub repo**, then `desk-runner`. Allow Railway to see the repo if it asks.
3. **Add a volume.** On the project canvas, tap **+ Create**, then **Volume**. Attach it to the `desk-runner` service
   with mount path `/data`. The desk finds the volume on its own. Without a volume, every redeploy forgets the
   trades. Live mode refuses to start without one.
4. **Variables.** Tap the service, then **Variables**. Add each one with **New Variable**:

   | variable | value |
   |---|---|
   | `TYPESAFE_API_KEY` | your TypeSafe key (Jev) |
   | `ADMIN_TOKEN` | make up a password, at least 16 characters. The Pause button asks for it. |
   | `LIVE_MODE` | `false` |
   | `JUPITER_API_KEY` | recommended: a free key from **portal.jup.ag**. Without one, Jupiter limits you harder. |
   | `SOLANA_RPC_URL` | recommended: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY` (free key at **helius.dev**) |
   | `PORT` | `8080` |
   | `GOAL_USD` | optional: draws the goal bar, e.g. `2000` |

   Then tap **Deploy** to apply the changes.
5. **Your permanent link.** Go to the service's **Settings**, then **Networking**, then **Generate Domain**. If it asks
   for a port, enter `8080` (the `PORT` you set). You get a link like `https://desk-runner-production-xxxx.up.railway.app`. It stays the
   same across redeploys.
6. Open the link. In Safari's **Share** menu, choose **Add to Home Screen**: it opens like an app.

If a variable is wrong, the page shows **STOPPED** and names the variable. Fix it in Variables and tap **Deploy**.

**Cost:** Railway's trial includes $5 of credit for 30 days. After that, the Hobby plan is $5 a month, which includes
$5 of usage. This desk mostly sleeps between cycles, so by my estimate it fits inside that. Jev costs about
$0.0006 a cycle, roughly 6¢ a day.

## 3. Watch paper mode first

Let it run a day or two. Watch the **trades** panel and the balance chart. Paper trades use real Jupiter quotes
both ways, so price impact and Jupiter's route fees are counted. Network fees, and any slippage between the quote
and a real fill, aren't. Real results will be a little worse.

## 4. Go live (real money)

1. In **Phantom**, add a **new** account: tap your account icon at the top left, then **Add Account**, then
   **Create New Account**. Use it only for the bot, and send it only SOL you can afford to lose.

   **How much it needs before it trades at all:** a ticket is 6% of the wallet × size_factor. With no X read, the
   factor is 0.60, so 3.6%. With a dark data source as well, it is 0.24, so 1.44%. Tickets under `MIN_TICKET_USD`
   ($5) are skipped. So with the defaults the wallet needs about **$140** of SOL to buy anything, and about $350
   to also take the dark-data picks. To start smaller, set `MIN_TICKET_USD` lower, e.g. `1`. Fees are small on
   Solana, but priority fees, Jupiter's swap fee and price impact take a bigger share of a tiny ticket.
2. Export that account's private key: Phantom **Settings**, then **Manage Accounts**, then the bot account, then
   **Show Private Key**. Phantom's menus move between versions, so look for "Show Private Key" if these names
   differ.
3. In Railway Variables, add `BOT_WALLET_KEY` and paste the key there. Paste it **only** into Railway: never into a
   chat, a note or a screenshot. Not even to me.
4. Change `LIVE_MODE` to `true` and tap **Deploy**. The badge turns red: **LIVE · REAL MONEY**.

The bot keeps 0.02 SOL in the wallet for fees (`FEE_RESERVE_SOL`). The bot's buys and sells show up in that Phantom
account like any other swap, so you can see them there, or sell by hand.

## The buttons (bottom of the page, need `ADMIN_TOKEN`)

| button | what it does |
|---|---|
| **Scan now** | runs the next scan right away instead of waiting out the 15 minutes (not while holding, not twice a minute) |
| **Test swap** | paper: asks Jupiter for real SOL→USDC→SOL quotes, sends nothing. Live: really swaps about `TEST_SWAP_USD` ($1) of SOL to USDC and back, with Solscan links for both transactions. Do this once after going live, before the bot trades a memecoin. |
| **Pause / Resume** | stop / restart new buys. RISK keeps managing an open position. |

On the floor the crawler (the spider) carries the coin being read to the seat reading it, tickets fly to the
shredder when a coin is cut and to the big screen when Jev is asked, and the screens show real numbers only: the
big monitor draws the coin's 5-minute candles from GeckoTerminal (fetched by your phone) or says it has none, the
desk monitors show the balance, fills, tickets, P&L and coins per scan. thresholds.py shows the coin under the
lens with its real value next to every rule, ✓ or ✗. The live feed lists every coin: what was cut and why, what Jev
was asked, what was bought and sold. Tap a coin to open its DexScreener chart.

## Several coins at once

The desk holds up to `MAX_POSITIONS` coins (default **5**) and keeps scanning while it has a free slot. Each coin
gets its own plan below. All coins together stay under `MAX_EXPOSURE_PCT` (default **25**%) of everything (cash +
what's in coins): a ticket that would go over is cut to the room left, or skipped. A coin it already holds is
skipped by the scan, and a coin it just sold is set aside for an hour, so it never buys straight back in.
`MAX_POSITIONS=1` gives back the one-coin desk.

Prices are shown as **market caps** everywhere (DexScreener's market cap, never FDV): what it bought at, where it
is now, its best, and the market cap at which each ladder step, the trailing stop and the stop-loss sell.

## Taking profit and cutting losses (RISK + WATCH)

While a coin is held, **WATCH** reads its real price and the last five minutes of buys and sells from DexScreener
every minute, keeps the best price since the buy, and acts on the first rule that applies (one action per check,
no action on a number it can't read):

| rule | default | what it does |
|---|---|---|
| stop-loss | `STOP_LOSS_PCT=30` | down 30% from the buy → sell everything |
| take-profit (all) | `TAKE_PROFIT_PCT=0` (off) | up X% → sell everything |
| max hold | `MAX_HOLD_HOURS=24` | held this long → sell everything |
| trailing stop | `TRAIL_ARM_PCT=20`, `TRAIL_PCT=15` | once it has been up 20%, a 15% drop from its best price sells everything: "looks like the top" |
| profit ladder | `SCALE_OUT=30:25,60:25,100:25` | at +30% sell 25% of the original bag, at +60% another 25%, at +100% another 25%; the last 25% rides on the trailing stop |
| breaking down | `WATCH_DROP_M5_PCT=8` | in a loss and down 16%+ in 5 minutes with sellers 1.5× buyers → sell everything |
| turning down | `WATCH_DROP_M5_PCT=8`, `WATCH_DERISK_PCT=50` | in profit, down 8%+ in 5 minutes with sellers 1.3× buyers → sell half of what's left (at most every 10 minutes) |
| spike | `WATCH_PUMP_M5_PCT=20`, `WATCH_PUMP_SELL_PCT=30` | up 20%+ in 5 minutes while 20%+ in profit → take 30% off (at most every 15 minutes) |

RISK's volume rule still runs every 5 minutes on top (volume ratio under 0.20 → sell everything). `WATCH=off`
turns the price manager off; `0` turns a single rule off; `SCALE_OUT=off` removes the ladder. Every sale is its
own row in TRADES (part sales are marked with the % sold); wins and losses count per coin, not per sale.
The POSITION · TRADE PLAN panel shows the plan live: entry and price now, best so far, which ladder steps are done,
the exact price the trailing stop and the stop-loss sell at, and MONEY: cash, what's in the coin, the worst case to
the stop, what's banked today and how much of the daily loss limit is left.

These are rules, not predictions: a coin can gap through a stop, and a trailing stop can sell just before a
second leg up. Test them in paper first.

## Stopping it

| you want | do this |
|---|---|
| no new buys | **Pause** on the dashboard (asks for `ADMIN_TOKEN`). RISK keeps watching the open position and still sells it. |
| out of the position now | sell the token in Phantom (it's your bot account), then Pause. |
| everything off | sell in Phantom first, then in Railway open the service's **Deployments** and **Remove** the active one. Nothing runs until you redeploy. |

## What it does each cycle (every 15 min, unless it's holding)

`free_kill` → `trade_kill` → `chain_kill` → `soft_kill` → `pick` → SIZE → FILLS → RISK

1. **Scan.** Trending and new Solana pools come from GeckoTerminal, and prices and volume from DexScreener. Arithmetic kills
   come first, because they cost nothing.
2. **Vet.** Only the survivors get a dossier: Solana RPC, mint and freeze authority, real top holders (pool vaults
   excluded), and GeckoTerminal. Jev answers the market and Solana sets. A missing fact is sent as `null` and is
   never treated as fine.
3. **Pick.** Jev chooses between the survivors, labelled c1, c2… and never by ticker. `worth_trading_at_all` must
   pass.
4. **Size.** The ticket is 6% of free cash × size_factor (×0.40 if a data source was dark, ×0.60 with no X read).
   It is never more than 2% of the pool, and never under `MIN_TICKET_USD`.
5. **Fills.** A Jupiter order is refused if the price impact is over `MAX_IMPACT_PCT`. The bot signs the swap with
   the bot key and executes it.
6. **Risk.** Every 5 min it compares recent volume to its 24h pace. Under 0.20 means the move is over, so it sells.
   No data three times in a row also sells.

If today's closed trades lose `MAX_DAILY_LOSS_PCT` (20%) of the day's opening bank, the desk pauses itself. It
stays paused until you tap **Resume**.

## All variables

| variable | default | what |
|---|---|---|
| `TYPESAFE_API_KEY` | none | needed: the desk can't ask Jev without it |
| `ADMIN_TOKEN` | none | 16+ characters; without it, the Pause button is off |
| `LIVE_MODE` | `false` | exactly `true` or `false` |
| `BOT_WALLET_KEY` | none | live only. The bot account's private key, in Phantom's export format. |
| `JUPITER_API_KEY` | none | recommended |
| `SOLANA_RPC_URL` | public endpoints | strongly recommended: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY` (free key at helius.dev). Public endpoints often refuse the holder lookup, and then every token is cut as `top_wallet_unknown`. |
| `GOAL_USD` | none | the goal bar |
| `PAPER_BANK_USD` | `1000` | paper mode's starting cash |
| `MIN_TICKET_USD` | `5` | no buy smaller than this |
| `MAX_IMPACT_PCT` | `3` | refuse fills with more price impact |
| `MAX_DAILY_LOSS_PCT` | `20` | pause the desk after losing this % of the day's opening bank |
| `STOP_LOSS_PCT` | `30` | sell everything if the position is down this much (`0` = off) |
| `FEE_RESERVE_SOL` | `0.02` | SOL the bot never spends |
| `RISK_POLL_SECONDS` | `300` | how often RISK checks an open position (at least 30) |
| `CYCLE_SECONDS` | `300` | time between scans (5 minutes) |
| `TRENDING_PAGES` | `1` | pages of GeckoTerminal trending pools scanned first (0 to 3; 0 = new pools only) |
| `TRENDING_WINDOW` | `1h` | what "trending" means: `5m`, `1h`, `6h` or `24h` |
| `PAUSED` | none | `true` starts the desk paused |
| `MAX_POSITIONS` | `5` | coins held at once (1 to 10) |
| `MAX_EXPOSURE_PCT` | `25` | all coins together, as % of cash + coins |
| `TEST_SWAP_USD` | `1` | size of the Test swap round trip, $0.50 to $20 |

| `DESK_TITLE` / `DESK_TAGLINE` | `GROKBOT + JEV` / `JEV PICKS, CODE TRADES` | the page's name and tagline |
| `HARD_<NAME>` | thresholds.py | override one HARD number, e.g. `HARD_MAX_AGE_HOURS=168` |
| `SOFT_<NAME>` | thresholds.py | override one SOFT limit, e.g. `SOFT_MOMENTUM_ALREADY_SPENT=0.75` |
| `SHAPE_MIN_CROWD`, `PICK_MIN_WORTH`, `PICK_MIN_CONF` | thresholds.py | override those three |

| `PRACTICE_MODE` | none | `true` (paper only): opens the gates that keep most coins out, so you can watch the desk buy and sell. It sets `SHAPE_MIN_CROWD=0.25`, `PICK_MIN_WORTH=0.20`, `PICK_MIN_CONF=0.30`, momentum 0.90, concentration 0.80, dev 0.80, liquidity fit 0.35, age 168h, top wallet 12%, top 10 80%. Ignored in live mode. Paper results under it say nothing about the real rules. |

Overrides change the desk's rules, in paper and in live alike. The thresholds.py panel on the page shows the
numbers in force and lists which ones came from the variables.

A variable that is set but isn't a valid number stops the desk and names the variable. It is never silently
replaced with the default.

## What is and isn't tested

Tested here, with no real network and fake Jupiter and RPC answers:
- the full paper loop: scan, cut, vet, pick, size, buy, hold, RISK close, and the P&L;
- the transaction signing, checked against ed25519;
- the wallet-key parsing;
- the variable checks;
- the pause endpoints;
- the dashboard on an iPhone-sized screen.

**Not tested yet:**
- the real Jupiter API with a real wallet;
- the TypeSafe SDK and FastAPI install on Railway;
- Railway itself.

Your first deploy is the first real run of those, which is one more reason to start in paper mode.

## Known limits

- Solana only, because that's what Jupiter swaps.
- X lookups are off, so every ticket takes the ×0.60 no-X cut.
- SIZE has no Kelly step: nothing measures an edge yet.
- Up to 5 coins at once (`MAX_POSITIONS`).
- The dashboard link is public, but read-only. Anyone with it can see the bot wallet's address and trades. They
  can't pause it or move funds.
- **FOMO:** it has no public trading API, and its terms forbid bots, so this desk trades through Jupiter instead.
  The tokens are the same on-chain Solana tokens.

## Running the tests (on a computer)

```
pip install -r requirements-dev.txt && playwright install chromium
cd tests && python -m pytest -q
```
