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
| who's behind it | **the bubble map**: who funded each of the biggest holders (read from each wallet's first transaction on the chain): the top 50 when `SOLANA_RPC_URL` is a Helius key (Helius lists every holder), else the 20 the plain RPC lists. With `BUBBLEMAPS_API_KEY` it uses Bubblemaps' own map instead: its clusters and the share it counts in bundles (4%+ = SKIP). A map that can't be read means no buy, for the sniper and the desk alike: it looks again later. Several top holders funded by one wallet are one person: 3+ of them holding 4%+, all clusters together 10%+, or 5+ top holders in clusters is a SKIP (DOGGO's ring, OnlyDoge's two hubs); a single small pair costs points. Holders paid out by an exchange (a funder with 1000+ transactions) aren't a cluster. Also: top holders RugCheck ties to the insiders (10%+ = SKIP), how much the dev / creator still holds, the dev wallet's history, a bundled launch (many wallets buying in the very first block), **copycats** (an older coin with the same ticker and 3x the liquidity), holders |
| where it is | age, market cap, liquidity, fresh launch / early runner / second leg, graduated from the curve |
| momentum now | every trade on the pool: different wallets buying in the last 5 min, buy dollars vs sell dollars, the biggest buy, volume speeding up, the price in the last 5 min |
| smart money | the wallets you follow: did they buy it, do they hold it |
| attention | socials, paid DexScreener boosts, which lists it's on, and a web search for posts on X, Reddit, TikTok, YouTube and news (only with `ANTHROPIC_API_KEY`) |
| Jev | the desk's read of the market shape, whether the move is spent, the holder exit risk. The sniper only asks Jev about a coin that is already close to a BUY, so its credits stay free for the desk |

**Narrative and hype, or no buy.** It only buys a coin with a story people are piling into right now, and attention
growing fast:

- **narrative**, any one of:
  - it's in one of the narratives trending on DexScreener right now (their "metas": a person, an AI wave, a viral
    character, with that theme's market cap move). It counts when the coin is listed in the narrative, or when the
    narrative's word is in its name (a coin launched ten minutes ago isn't listed yet).
  - **a theme several new coins are riding**: two other coins out in the last 3 hours share a word of its name (clones
    with the same ticker don't count). For one of the big meme families, one other coin is enough: $MarsX out, then
    $MARSDOGE.
  - **a big meme family** (Doge, Pepe, Trump, Elon / Mars, cats, dogs, AI, Moo Deng, stonks, CZ / SAFU...) with its own
    X account.
  - **the crowd is the story**: 1,500+ holders, growing 1,500+ an hour, with hype 70+. Something is pulling people in
    even if its name doesn't say what.
  - the web search says who or what it's about and people are talking about it (needs `ANTHROPIC_API_KEY`).
  - a specific theme tag (not just "Pump Fun") plus its own socials, a community takeover, or a paid profile with X and
    a site.
- **hype, out of 100** (needs 50, `SNIPE_MIN_HYPE`): different wallets buying in the last 5 minutes, new holders an
  hour (between its own looks, or since launch), volume speeding up, the price climbing this hour, trending lists and
  narratives, and talk online.

**Room for 2x, 3x, 5x.** Every report has an UPSIDE box: the market caps a 2x, 3x and 5x would mean, how far under
the biggest coin of its own narrative it sits (traders buy the next coin of a hot story while its leader is far
above), where coins of its size usually top out, and how long a 2x takes at this hour's pace. With less than 2x of
room (`SNIPE_MIN_UPSIDE_X`) it doesn't buy. It's a read of the room, not a promise.

A coin that's safe but has no story, or whose hype is still slow, or with no room left, is a WATCH: it gets looked at again every few
minutes while it builds. `SNIPE_NEED_NARRATIVE=off` drops the narrative rule. These are the sniper's rules; the desk's
normal coins don't need them.

A serious rug-safety failure (mint or freeze authority still on, RugCheck flagging a rug, honeypot, copycat, creator
history, a transfer fee or hook...), liquidity unlocked on a coin under a day old, a copycat, or "nobody can sell" is
a **SKIP**, whatever the score. RugCheck's minor warnings (a few big holders, low liquidity, and so on) only cost 6
points each (at most 18), and unlocked liquidity on an older coin costs 10. A big run alone is not a SKIP (on a coin
under a day old the 24h change is "since launch", so every coin that made it shows +1000%): it's a SKIP only when it
ran 300%+ and is falling this hour (−15% or worse, with the last 5 minutes red). A small red hour costs 6 points.
Unknown is never a
pass: a check that couldn't be read is a **WATCH**. A **BUY** needs 70/100 (`SNIPE_BUY_SCORE`) and buyers ahead
right now. With **buy if it looks good** ticked, a BUY is bought at once; untick it to only check, then **Buy it**
buys at half size (never a SKIP).

**The fast lane.** Every minute (`SNIPE_SCAN_SECONDS`), even with every slot full, it looks at the coins inside the
narratives trending now, new pools, GeckoTerminal's 5-minute trending,
DexScreener's newest profiles, boosts and community takeovers, and every buy by a wallet you follow. A cheap first
look decides which two get the full check. **OFF** = only your own checks, **WATCH** = finds and checks, never buys,
**AUTO** = buys the BUYs that pass a second look. Paper starts on AUTO, live starts on WATCH (`SNIPE_MODE` sets it).

**A second look before the money goes in.** A fast-lane BUY isn't bought on the spot: it looks again a minute later
(`SNIPE_CONFIRM_SECONDS`) and buys only if the price held (not down 4%+, `SNIPE_CONFIRM_DROP_PCT`), didn't rocket 30%+
in that minute (the top of a candle), and sellers didn't take over. A coin that fails is looked at again in a few
minutes. A coin up 30%+ in 5 minutes (`SNIPE_MAX_M5_PCT`) waits for the candle to cool, desk picks included. Paid
DexScreener boosts and profiles earn no points (devs pay for those to sell into the attention). Coins under
10 minutes old (`SNIPE_MIN_AGE_MINUTES`) aren't bought: the first minutes are the dev's and the bots' dump.

**Resting after a losing streak.** After 2 sniper coins in a row closed at a loss (`SNIPE_LOSS_STREAK`), the fast lane
doesn't buy for 30 minutes after the last one (`SNIPE_COOLDOWN_MINUTES`): when new coins keep dumping, the market is
cold. It keeps checking, and your own checks still buy. The fast lane box says when it buys again.

**Wallets you follow.** Add any trader's Solana wallet on the page (or `SMART_WALLETS=address:name,address:name`).
When one of them buys a coin, that coin is checked within a minute, and a followed buy adds up to 24 points.

**Why no buy.** The fast lane box counts, for the last hour, every coin it checked in full and why it didn't buy:
no narrative, hype too slow, no 2x room, rug risk, copycat, slots full... so a quiet bot always says why.

**Sell now.** Each coin in POSITIONS has a **Sell all now** button (it still won't sell into a quote far under the
market). Use it to free a slot.

**In and out.** The sniper's coins get their own plan: stop at −25%, sell 30% at +50%, 30% at 2x and 20% at 3x, a 20%
trailing stop once it's been up 30%, **out at −12% if it's still falling 10%+ in 5 minutes in its first 30 minutes**
(the move failed: no riding it down to the stop), and **out if it hasn't made +10% within 20 minutes** (no momentum,
no stay). A loss rule only sells once Jupiter's real quote agrees: a wick on the price feed doesn't sell.
At 12 hours it sells, unless buyers are still ahead and it's still rising: then it rides up to 3x longer. Every
number is a variable (`SNIPE_*` below).

**What it learned.** Every closed sniper trade keeps the signals it was bought on. The page shows, for each signal,
how many coins, how many won, the average result and the P&L, so you can see which signals actually pay and raise or
lower the bar. It doesn't change its own rules: you do, with the variables.

**Honest limits.** No bot can promise to beat the best traders. Most new memecoins go to zero, and the earlier you
buy, the less there is to check. The sniper can't see private Telegram groups or a celebrity's post before
everyone else. It only reads X, TikTok and Reddit through the web search, which needs `ANTHROPIC_API_KEY` and costs
about $0.01 per search plus tokens (at most 3 searches a check, `BUZZ_PER_HOUR` checks an hour). Run it on paper first.

## Two bots, their own slots

The desk runs **two bots side by side**:

- **the desk** (normal coins): every `CYCLE_SECONDS` it scans trending and new pools, cuts with its own rules
  (thresholds.py), asks Jev, and buys Jev's pick, like it always did. Before a cent goes in, its pick also passes the
  sniper's **safety** checks only (no serious rug danger, no copycat, people able to sell, not already run and
  cooling, not dumping right now, a score of at least 25). Not the sniper's early-coin rules: a normal coin
  doesn't need a trending narrative, hype or a 72-hour age limit.
- **the sniper** (early coins): every minute, the fast lane, with its narrative, hype and 2x-room rules. It reads
  new pools, GeckoTerminal's 5-minute trending, **pump.fun's busiest coins** (GeckoTerminal's top pools on pump.fun,
  still on the curve), DexScreener's narratives, profiles and takeovers, and the wallets you follow.

The desk reads **older coins**: GeckoTerminal's trending pools over the last hour, 6 hours and day (`DESK_TRENDING`),
and the coins inside the narratives trending on DexScreener right now (up to 60, `DESK_METAS`; the 12 hottest
narratives, read every 10 minutes), an hour old or more (`HARD_MIN_AGE_MINUTES`, 60). Minutes-old launches are the sniper's; `DESK_NEW_POOL_PAGES=1` adds
new pools back to the desk.

**FOMO, Phantom and pump.fun callouts.** FOMO's terms forbid automated access and it has no public list, so the bots
can't read its trending tab. Phantom publishes no market-data API (its developer docs cover the wallet, not its
trending list). DexScreener's official API has no "trending pairs" ranking either, but the bots read everything it does
publish: its trending narratives, new and updated profiles, boosts, community takeovers and every pair's numbers. Pump.fun's callouts live inside its app as push notifications to a caller's followers, with no
public feed either. What works instead: add the wallets of the callers you trust under **Wallets you follow**. A
caller buys the coin they call, so the sniper checks it within a minute of their buy, with every rug check and the
bubble map, before it buys anything.

They split the slots so one can never crowd out the other: with `MAX_POSITIONS=5`, 3 are the desk's and 2 the
sniper's (`DESK_SLOTS`, `SNIPE_SLOTS`). A coin you paste and tell it to buy may take any free slot. The POSITIONS
panel says which bot bought each coin, and the bottom section has a box for each bot saying what it's doing.

If you want the desk trading as much as it did the first night, `PRACTICE_MODE=true` opens its gates again (paper
only); the safety checks now run on every pick either way.

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
every 10 seconds (`WATCH_SECONDS`), also while a scan is running, keeps the best price since the buy, and acts on the first rule that applies (one action per check,
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
now. A SKIP, a coin dumping right now (down 8% or more in 5 minutes, or sellers well ahead of buyers over the last
15 minutes), or a score under 25 (`DESK_GATE_SCORE`) means no buy. A coin with a few more sellers than buyers is
still bought: Jev already liked it. Jev isn't asked twice about the desk's own pick. When the checks stop a pick, that
coin sits out an hour and the desk scans again a minute later (not five), so the other coins that passed get their turn.
`DESK_GATE=off` turns it off.

**GeckoTerminal busy.** GeckoTerminal answers "too many requests" (429) now and then, and Railway's shared addresses
make it more likely. After one, it gets a minute of rest. Meanwhile a Solana coin's dossier reads the chain itself
(mint and freeze authority, Token-2022 traps, the biggest wallets, the top 10) and goes on with GeckoTerminal's extras
(holder count, dev share, X handle) left empty, never guessed; the feed says so. The sniper uses DexScreener's 5-minute
counts and RugCheck's holder count for that minute.

**Saved between deploys.** The bank, trades, benches and what the bots learned live in `DATA_DIR`. On Railway that
must be a **volume** (mount path `/data`), or every upload starts over at the paper bank and Day 1. Without one, the
page shows a red NOT SAVED bar.

`WATCH=off` turns the price manager off; `0` turns a single rule off; `SCALE_OUT=off` removes the ladder. Every sale is its
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
   pass. When Jev has no clear favourite (its confidence under `PICK_MIN_CONF`), it still buys Jev's top choice, with
   half the ticket: every survivor passed every check, and buying none of them left the desk idle all evening.
   A scan reads up to 3 coins with GeckoTerminal's dossier, then up to 3 more Solana coins from the chain alone, so it
   doesn't stop at the 4th good coin.
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
| `SOLANA_RPC_URL` | public endpoints | strongly recommended: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY` (free key at helius.dev; the `beta.helius-rpc.com` gateway is a public beta and refuses heavy holder lookups more often). Public endpoints often refuse the holder lookup; then the top holders come from RugCheck's list instead, and if that fails too the coin is cut as `top_wallet_unknown`. |
| `RPC_MAX_PER_SEC` | `8` | Solana RPC calls a second at most, all together (the bubble map reads many wallets; free plans limit calls a second) |
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
| `DESK_TRENDING` | `1h,6h,24h` | the trending windows the desk reads (any of 5m, 1h, 6h, 24h) |
| `TRENDING_PAGES` | `1` | pages of each trending window (0 to 3) |
| `DESK_NEW_POOL_PAGES` | `0` | pages of brand-new pools the desk also reads (0 to 3; new coins are the sniper's) |
| `DESK_METAS` | `60` | coins from DexScreener's trending narratives the desk reads too (0 = none) |
| `TRENDING_WINDOW` | `1h` | what "trending" means: `5m`, `1h`, `6h` or `24h` |
| `PAUSED` | none | `true` starts the desk paused |
| `MAX_POSITIONS` | `5` | coins held at once (1 to 10) |
| `MAX_EXPOSURE_PCT` | `25` | all coins together, as % of cash + coins |
| `TEST_SWAP_USD` | `1` | size of the Test swap round trip, $0.50 to $20 |
| `DESK_TITLE` / `DESK_TAGLINE` | `GROKBOT + JEV` / `JEV PICKS, CODE TRADES` | the page's name and tagline |
| `HARD_<NAME>` | thresholds.py | override one HARD number, e.g. `HARD_MAX_AGE_HOURS=168` |
| `SOFT_<NAME>` | thresholds.py | override one SOFT limit, e.g. `SOFT_MOMENTUM_ALREADY_SPENT=0.75` |
| `SHAPE_MIN_CROWD`, `PICK_MIN_WORTH`, `PICK_MIN_CONF` | thresholds.py | override those three |
| `WATCH_SECONDS` | `10` | how often WATCH reads the price of each coin held (5 at least) |
| `TRAIL_LOCK_PCT` / `SNIPE_TRAIL_LOCK_PCT` | `8` / `10` | once the trail is armed, sell rather than fall back under this gain |
| `DESK_GATE` / `DESK_GATE_SCORE` | `on` / `25` | every desk pick must pass the sniper's safety checks, not be dumping, and score at least this |
| `DESK_SLOTS` / `SNIPE_SLOTS` | 60% / 40% of `MAX_POSITIONS` (3 / 2) | the slots each bot may fill |
| `SNIPE_MODE` | `auto` paper, `watch` live | the fast lane: `off`, `watch` (never buys) or `auto` |
| `SNIPE_BUY_SCORE` | `70` | the score a BUY needs |
| `SNIPE_SCAN_SECONDS` | `60` | how often the fast lane looks |
| `SNIPE_MIN_AGE_MINUTES` / `SNIPE_MAX_AGE_HOURS` | `10` / `72` | how young and how old a coin it looks at |
| `SNIPE_CONFIRM_SECONDS` / `SNIPE_CONFIRM_DROP_PCT` | `60` / `4` | the second look before a fast-lane buy, and how far the price may slip in it (`0` = buy at once) |
| `SNIPE_MAX_M5_PCT` | `30` | up this much in 5 minutes = the top of a candle: wait (desk picks too) |
| `SNIPE_LOSS_STREAK` / `SNIPE_COOLDOWN_MINUTES` | `2` / `30` | after this many sniper losses in a row, no fast-lane buys for this long |
| `SNIPE_FAIL_PCT` / `SNIPE_FAIL_M5_PCT` / `SNIPE_FAIL_MINUTES` | `12` / `10` / `30` | the move failed: out at −12% while still falling 10% in 5 min, in the first 30 min |
| `SNIPE_MIN_LIQUIDITY_USD` | `8000` | thinner than this is too hard to get out of |
| `SNIPE_MIN_MCAP_USD` / `SNIPE_MAX_MCAP_USD` | `20000` / `30000000` | the market cap window |
| `SNIPE_MAX_TOP_WALLET_PCT` / `SNIPE_MAX_TOP10_PCT` / `SNIPE_MAX_DEV_PCT` | `8` / `45` / `10` | rug lines |
| `SNIPE_BUNDLE_WALLETS` | `8` | this many wallets in the launch block = a bundled launch |
| `SNIPE_STOP_LOSS_PCT`, `SNIPE_SCALE_OUT`, `SNIPE_TRAIL_ARM_PCT`, `SNIPE_TRAIL_PCT` | `25`, `50:30,100:30,200:20`, `30`, `20` | the sniper's exits (sells at +50%, 2x and 3x) |
| `SNIPE_STALL_MINUTES` / `SNIPE_STALL_GAIN_PCT` | `20` / `10` | out if it hasn't made +10% within 20 min |
| `SNIPE_MAX_HOLD_HOURS` / `SNIPE_RIDE_X` | `12` / `3` | max hold, and how much longer it rides while buyers lead |
| `SNIPE_NEED_NARRATIVE` / `SNIPE_MIN_HYPE` | `on` / `50` | buy only coins with a narrative, and hype at least this |
| `SNIPE_MIN_UPSIDE_X` | `2` | buy only with room for at least this many x |
| `BUBBLEMAPS_API_KEY` | none | Bubblemaps' own map (key from pro.bubblemaps.io, a paid plan; 25 of its credits a coin, read at most every 10 minutes) |
| `SNIPE_MAP` | `on` | the bubble map check (who funded the top holders); read for desk picks, your own checks and coins close to a BUY |
| `SNIPE_MAP_CLUSTER_PCT` / `SNIPE_MAP_TOTAL_PCT` / `SNIPE_MAP_MAX_WALLETS` | `4` / `10` / `5` | SKIP when 3+ holders of one funder hold this %, all clusters hold this %, or this many top holders are in clusters |
| `SNIPE_INSIDER_PCT` | `10` | SKIP when top holders RugCheck ties to the insiders hold this % |
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
