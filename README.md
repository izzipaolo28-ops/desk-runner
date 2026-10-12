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
   | `GOAL_USD` | optional: a starting goal for the bar at the top, e.g. `2000` (you can also set it from the page) |

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
| **Scan now** | runs the next scan right away instead of waiting out the 5 minutes (not when every slot is full, not twice a minute) |
| **Test swap** | paper: asks Jupiter for real SOL→USDC→SOL quotes, sends nothing. Live: really swaps about `TEST_SWAP_USD` ($1) of SOL to USDC and back, with Solscan links for both transactions. Do this once after going live, before the bot trades a memecoin. |
| **Pause / Resume** | stop / restart new buys. RISK and WATCH keep managing the coins already held. |
| **✎ goal** (top bar) | set your goal in dollars, like `10000` or `10k`; empty clears it |

## The page

- **Top bar:** your bank (cash + coins at their last value) → your goal, the bar, and the % of the goal reached.
- **Under BALANCE:** one banner per coin held: market cap at the buy → market cap now, % up or down, and its P&L.
  Tap one and it goes on the big chart.
- **JEV · ANALYSIS:** the big chart. The chips above it pick the coin: every coin held, plus the coin being
  scanned. It opens on **● LIVE**, a candle chart built from every trade on the coin:
  - **Top strip:** the coin's logo, name and age, its address (tap to copy), then Market cap, **Your entry MC**
    (with % since the buy), **P&L now**, Price, 24H change, 24H Vol., Liquidity, Holders.
  - **Toolbar:** candle size **1s / 5s / 15s / 1m / 5m**, candles or a line, **Price / MCap**.
  - **On the chart:** your entry market cap as a white dashed line, the stop-loss and the next take-profit step,
    a **B** where the desk bought and an **S** where it sold, volume bars, and the price now on the right.
    Drag sideways to go back in time (**⇥ LIVE** returns), tap or hover a candle for its numbers, zoom with
    **− / +**, **log** for a log scale.
  - **Where the numbers come from:** trades from GeckoTerminal every 3.5 seconds, DexScreener's price and the
    coin's info every 2.5 seconds, holders every 5 minutes. Your phone fetches them, and only while the chart is
    on screen. LAST TRADES under it lists each trade with a Solscan link.
  - **Under the chart:** **● LIVE**, **Candles** (DexScreener's own chart), **Open in FOMO**, **DexScreener ↗**,
    **Solscan ↗**. The small charts on the side are the latest other coins; tap one to look at it for 2 minutes.
- **POSITIONS · TRADE PLAN:** one line per coin (MC in → now, %, P&L) so all of them fit; tap a line for that
  coin's whole plan. MONEY under it covers every coin together.
- **FOMO:** FOMO's web app needs your login and has no public page or embed for a coin, so FOMO's own chart can't
  sit inside this page; the LIVE chart is built from the same on-chain trades instead. **Open in FOMO**
  copies the coin's address (paste it in FOMO's search). If you set `FOMO_LINK` to FOMO's link for one coin with
  `{mint}` where the address goes, the button opens that coin directly.

On the floor the seats walk around their desks and visit each other; when one has work it runs back to its desk.
CHIEF does rounds of every desk from the head of the floor and carries each PICK over to SIZE. The crawler (the
spider) carries the coin being read to the seat reading it, tickets fly to the
shredder when a coin is cut and to the big screen when Jev is asked, and the screens show real numbers only: the
big monitor draws the coin's 5-minute candles from GeckoTerminal (fetched by your phone) or says it has none, the
desk monitors show the balance, fills, tickets, P&L and coins per scan. thresholds.py shows the coin under the
lens with its real value next to every rule, ✓ or ✗. The live feed lists every coin: what was cut and why, what Jev
was asked, what was bought and sold. Tap a coin to open its DexScreener chart.

## The sniper (SNIPER · CHECK A COIN, near the bottom of the page)

**Check a coin.** Paste a coin address, or any link with one in it (DexScreener, pump.fun, GMGN, Birdeye, Solscan),
and tap **Check**. Within a minute you get a report and a score out of 100:

| group | what it checks |
|---|---|
| rug safety | mint and freeze authority revoked (from the chain itself), Token-2022 traps, the biggest wallet and the top 10 (pools excluded), RugCheck's report (rugged, its danger list), liquidity locked or burned (or still on the bonding curve), people can actually sell |
| who's behind it | how much the dev / creator still holds, the dev wallet's history (an old busy wallet vs one made right before the launch), RugCheck's insider wallets, a bundled launch (many wallets buying in the very first block), **copycats** (an older coin with the same ticker and 3x the liquidity = this one is a knock-off), holders |
| where it is | age, market cap, liquidity, fresh launch / early runner / second leg, graduated from the curve |
| momentum now | every trade on the pool: different wallets buying in the last 5 min, buy dollars vs sell dollars, the biggest buy, volume speeding up, the price in the last 5 min |
| smart money | the wallets you follow: did they buy it, do they hold it |
| attention | socials, paid DexScreener boosts, which lists it's on, and a web search for posts on X, Reddit, TikTok, YouTube and news (only with `ANTHROPIC_API_KEY`) |
| Jev | the desk's read of the market shape, whether the move is spent, the holder exit risk |

**Narrative and hype, or no buy.** It only buys a coin with a story people are piling into right now, and attention
growing fast:

- **narrative**: the coin is in one of the narratives trending on DexScreener right now (their "metas": a person, an AI
  wave, a viral character, a meme family, with that theme's market cap move), or the web search says who or what it's
  about and people are talking about it, or it carries a specific theme tag (not just "Pump Fun") plus its own socials.
- **hype, out of 100** (needs 50, `SNIPE_MIN_HYPE`): different wallets buying in the last 5 minutes, new holders an
  hour (between its own looks, or since launch), volume speeding up, the price climbing this hour, trending lists and
  narratives, and talk online.

A coin that's safe but has no story, or whose hype is still slow, is a WATCH: it gets looked at again every few
minutes while it builds. `SNIPE_NEED_NARRATIVE=off` drops the narrative rule. The same rules apply to the desk's own
picks.

Any single rug-safety failure, a copycat, or "nobody can sell" is a **SKIP**, whatever the score. Unknown is never a
pass: a check that couldn't be read is a **WATCH**. A **BUY** needs 70/100 (`SNIPE_BUY_SCORE`) and buyers ahead
right now. With **buy if it looks good** ticked, a BUY is bought at once; untick it to only check, then **Buy it**
buys at half size (never a SKIP).

**The fast lane.** Every minute (`SNIPE_SCAN_SECONDS`), even with every slot full, it looks at the coins inside the
narratives trending now, new pools, GeckoTerminal's 5-minute trending,
DexScreener's newest profiles, boosts and community takeovers, and every buy by a wallet you follow. A cheap first
look decides which two get the full check. **OFF** = only your own checks, **WATCH** = finds and checks, never buys,
**AUTO** = buys every BUY. Paper starts on AUTO, live starts on WATCH (`SNIPE_MODE` sets it).

**Wallets you follow.** Add any trader's Solana wallet on the page (or `SMART_WALLETS=address:name,address:name`).
When one of them buys a coin, that coin is checked within a minute, and a followed buy adds up to 24 points.

**In and out.** The sniper's coins get their own plan: stop at −25%, sell 50% at +50% and 25% at +100%, a 20%
trailing stop once it's been up 30%, and **out if it hasn't made +10% within 20 minutes** (no momentum, no stay).
At 12 hours it sells, unless buyers are still ahead and it's still rising: then it rides up to 3x longer. Every
number is a variable (`SNIPE_*` below).

**What it learned.** Every closed sniper trade keeps the signals it was bought on. The page shows, for each signal,
how many coins, how many won, the average result and the P&L, so you can see which signals actually pay and raise or
lower the bar. It doesn't change its own rules: you do, with the variables.

**Honest limits.** No bot can promise to beat the best traders. Most new memecoins go to zero, and the earlier you
buy, the less there is to check. The sniper can't see private Telegram groups or a celebrity's post before
everyone else. It only reads X, TikTok and Reddit through the web search, which needs `ANTHROPIC_API_KEY` and costs
about $0.01 per search plus tokens (at most 3 searches a check, `BUZZ_PER_HOUR` checks an hour). Run it on paper first.

## Several coins at once

The desk holds up to `MAX_POSITIONS` coins (default **5**) and keeps scanning while it has a free slot. Each coin
gets its own plan below. All coins together stay under `MAX_EXPOSURE_PCT` (default **25**%) of everything (cash +
what's in coins): a ticket that would go over is cut to the room left, or skipped. A coin it already holds is
skipped by the scan. A coin it sold at a profit is set aside for three hours and a coin it lost money on for a day,
so it never chases a coin back up or buys a loser twice.
`MAX_POSITIONS=1` gives back the one-coin desk.

Prices are shown as **market caps** everywhere (DexScreener's market cap, never FDV): what it bought at, where it
is now, its best, and the market cap at which each ladder step, the trailing stop and the stop-loss sell.

## Taking profit and cutting losses (RISK + WATCH)

While a coin is held, **WATCH** reads its real price and the last five minutes of buys and sells from DexScreener
every 20 seconds (`WATCH_SECONDS`), also while a scan is running, keeps the best price since the buy, and acts on the first rule that applies (one action per check,
no action on a number it can't read):

| rule | default | what it does |
|---|---|---|
| stop-loss | `STOP_LOSS_PCT=30` | down 30% from the buy → sell everything |
| take-profit (all) | `TAKE_PROFIT_PCT=0` (off) | up X% → sell everything |
| max hold | `MAX_HOLD_HOURS=24` | held this long → sell everything |
| profit lock | `TRAIL_LOCK_PCT=8` | once it has been up 20%, it never lets it fall back under +8%: sells everything there instead of giving the gain back |
| trailing stop | `TRAIL_ARM_PCT=20`, `TRAIL_PCT=15` | once it has been up 20%, a 15% drop from its best price sells everything: "looks like the top" |
| profit ladder | `SCALE_OUT=30:25,60:25,100:25` | at +30% sell 25% of the original bag, at +60% another 25%, at +100% another 25%; the last 25% rides on the trailing stop |
| breaking down | `WATCH_DROP_M5_PCT=8` | in a loss and down 16%+ in 5 minutes with sellers 1.5× buyers → sell everything |
| turning down | `WATCH_DROP_M5_PCT=8`, `WATCH_DERISK_PCT=50` | in profit, down 8%+ in 5 minutes with sellers 1.3× buyers → sell half of what's left (at most every 10 minutes) |
| spike | `WATCH_PUMP_M5_PCT=20`, `WATCH_PUMP_SELL_PCT=30` | up 20%+ in 5 minutes while 20%+ in profit → take 30% off (at most every 15 minutes) |

RISK's volume rule still runs every 5 minutes on top: volume ratio under 0.20 on **two checks in a row** sells
everything. It only applies to a pool more than 2 hours old and 20 minutes after the buy (five minutes of volume is
noise, and a pump.fun coin moving to its own pool pauses trading for a few minutes).

**Never sold at a bad price.** Before any sale, the quote is compared with the market price. A quote under half of
what the market prices the coins at, while the pool still has money in it, is a routing hiccup (a coin moving to its
pool, a route that isn't ready): it doesn't sell, and tries again on the next check. A real rug (the pool emptied)
still sells for whatever there is, and after 20 minutes of bad quotes it sells anyway.

**Every desk pick gets the sniper's checks.** Before the desk buys its own pick, the sniper runs its full check:
RugCheck, locked liquidity, the dev wallet, bundles, copycats, whether people can sell, buyers against sellers right
now. A SKIP, sellers ahead, or a score under 50 (`DESK_GATE_SCORE`) means no buy. `DESK_GATE=off` turns it off. `WATCH=off`
turns the price manager off; `0` turns a single rule off; `SCALE_OUT=off` removes the ladder. Every sale is its
own row in TRADES (part sales are marked with the % sold); wins and losses count per coin, not per sale.
The POSITIONS · TRADE PLAN panel shows each coin's plan live: entry and price now, best so far, which ladder steps are done,
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

## What it does each cycle (every 5 min, while a slot is free)

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
| `GOAL_USD` | none | a starting goal for the top bar (a goal set from the page wins) |
| `FOMO_LINK` | none | FOMO's link for one coin with `{mint}` for the address, e.g. copied from FOMO's share button; must start with `https://` |
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
| `WATCH_SECONDS` | `20` | how often WATCH reads the price of each coin held (10 at least) |
| `TRAIL_LOCK_PCT` / `SNIPE_TRAIL_LOCK_PCT` | `8` / `10` | once the trail is armed, sell rather than fall back under this gain |
| `DESK_GATE` / `DESK_GATE_SCORE` | `on` / `50` | every desk pick must pass the sniper's checks and score at least this |
| `SNIPE_MODE` | `auto` paper, `watch` live | the fast lane: `off`, `watch` (never buys) or `auto` |
| `SNIPE_BUY_SCORE` | `70` | the score a BUY needs |
| `SNIPE_SCAN_SECONDS` | `60` | how often the fast lane looks |
| `SNIPE_MIN_AGE_MINUTES` / `SNIPE_MAX_AGE_HOURS` | `3` / `72` | how young and how old a coin it looks at |
| `SNIPE_MIN_LIQUIDITY_USD` | `8000` | thinner than this is too hard to get out of |
| `SNIPE_MIN_MCAP_USD` / `SNIPE_MAX_MCAP_USD` | `20000` / `30000000` | the market cap window |
| `SNIPE_MAX_TOP_WALLET_PCT` / `SNIPE_MAX_TOP10_PCT` / `SNIPE_MAX_DEV_PCT` | `8` / `45` / `10` | rug lines |
| `SNIPE_BUNDLE_WALLETS` | `8` | this many wallets in the launch block = a bundled launch |
| `SNIPE_STOP_LOSS_PCT`, `SNIPE_SCALE_OUT`, `SNIPE_TRAIL_ARM_PCT`, `SNIPE_TRAIL_PCT` | `25`, `50:50,100:25`, `30`, `20` | the sniper's exits |
| `SNIPE_STALL_MINUTES` / `SNIPE_STALL_GAIN_PCT` | `20` / `10` | out if it hasn't made +10% within 20 min |
| `SNIPE_MAX_HOLD_HOURS` / `SNIPE_RIDE_X` | `12` / `3` | max hold, and how much longer it rides while buyers lead |
| `SNIPE_NEED_NARRATIVE` / `SNIPE_MIN_HYPE` | `on` / `50` | buy only coins with a narrative, and hype at least this |
| `SMART_WALLETS` | none | wallets to follow: `address:name,address:name` (or add them on the page) |
| `ANTHROPIC_API_KEY` | none | turns on the web search for buzz (X, Reddit, TikTok, news), from console.anthropic.com |
| `BUZZ_PER_HOUR` / `BUZZ_MODEL` | `6` / `claude-haiku-5-5` | web-searched checks per hour (your own checks may use up to 30) |
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
