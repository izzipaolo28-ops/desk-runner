# Desk Runner

The desk, the bot and the DESK FLOOR dashboard in one Railway service. It scans Solana tokens, asks Jev through your
question sets, buys the pick through Jupiter, then RISK sells it when the volume dies. Up to 8 coins at once (`MAX_POSITIONS`).

It starts on **FAKE money**: real Jupiter quotes, nothing sent, no wallet needed. The **MONEY** box on the page
switches it to **REAL money** once it has a bot wallet (section 4), and back.

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

## 4. Real money: the FAKE / REAL switch

The **MONEY** box at the top of the page has two buttons: **FAKE MONEY** (a paper bank on real prices, nothing real
is bought) and **REAL MONEY** (the bots trade the SOL in the **bot wallet**). You switch between them any time; your
choice survives a redeploy. They need your `ADMIN_TOKEN`.

**How the money moves.** The bots never touch your own wallet. They trade a separate **bot wallet** whose key sits in
Railway. Your wallet (Phantom) only connects to the page:
- **Connect Phantom** makes your wallet the only place withdrawals can ever go. Phantom asks you to sign a message (a
  signature, not a transaction: it costs nothing and moves nothing). On an iPhone, the button opens this page inside
  Phantom's own browser, where your wallet can connect. Once set, it can't be changed from the page: to change it, put
  the new address in Railway's variables as `OWNER_WALLET`.
- **Send from my wallet** funds the bot wallet: type an amount of SOL, and Phantom shows the transfer on its own screen
  for you to approve. Before it does, the page reads the transfer back and checks it goes from your wallet to the bot
  wallet, for the amount you typed. You can also copy the bot wallet's address and send from any wallet app.
- **Withdraw to my wallet** sends SOL from the bot wallet back to your wallet (a number, or `all`). While REAL is on or
  real coins are held, it keeps 0.02 SOL there to pay the fees of the next sales. The smallest transfer is 0.001 SOL.
  A withdrawal waits for any real buy or sale in progress, and a second one waits until the first has landed.
- **The strongest lock:** put your own wallet's address in Railway's variables as `OWNER_WALLET`. Then withdrawals can
  only go there, even for someone who learned your `ADMIN_TOKEN` (anyone with the token could otherwise connect their
  wallet first, before you do).

**Setting up the bot wallet (once):**
1. In **Phantom**, add a **new** account: tap your account icon at the top left, then **Add Account**, then
   **Create New Account**. It's only for the bot.
2. Export that account's private key: Phantom **Settings**, then **Manage Accounts**, then the bot account, then
   **Show Private Key**. (Phantom's menus move between versions: look for "Show Private Key".)
3. In Railway Variables, add `BOT_WALLET_KEY` and paste the key there. Paste it **only** into Railway: never into a
   chat, a note, a screenshot or this page. Not even to me. Tap **Deploy**.
4. The page now shows the bot wallet's address. Fund it from your wallet (above), then tap **REAL MONEY**, read what it
   says, and type `REAL`.

**What REAL needs, and its limits.** The switch refuses until: there's a bot wallet, the service has its Railway
volume, `PRACTICE_MODE` is off, and the bot wallet holds SOL. Then:
- no single buy over **$25** (`REAL_MAX_TICKET_USD`), however much is in the wallet;
- it stops buying for the day at **-20%** (`MAX_DAILY_LOSS_PCT`); real and fake money each keep their own day, so
  switching back and forth never wipes the day's real losses;
- a ticket is 6% of the bot wallet x its size factor, and tickets under `MIN_TICKET_USD` ($5) are skipped: with the
  defaults it needs about $150 of SOL to buy anything, more to take the half-size early bets. To start smaller, set
  `MIN_TICKET_USD` lower, e.g. `2` (fees take a bigger share of a tiny ticket).
- **Test swap** (bottom of the page) swaps about $1 of the bot wallet's SOL to USDC and back: do it once after the
  first funding, to prove the wallet can trade.

The switch happens in the bot's own loop, between two steps (within seconds, or once a scan running at that moment
ends): a buy sized on one money is never filled with the other. Switching back to FAKE: new buys are paper, and any
real coins still held are managed and sold for real. Switching to REAL: paper coins still held finish on paper. A desk
pick waiting for its entry, a "buy it" you tapped, and a test swap you asked for are all dropped when the money
switches: ask again on the new money.
`LIVE_MODE=true` in Railway still starts it on real money, until you pick on the page.

Memecoins can go to zero. Only fund the bot wallet with money you can lose. No setting makes a 2x certain.

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
| who's behind it | **the bubble map**: who funded each of the biggest holders (read from each wallet's first transaction on the chain): the top 50 when `SOLANA_RPC_URL` is a Helius key (Helius lists every holder), else the 20 the plain RPC lists. With `BUBBLEMAPS_API_KEY` it uses Bubblemaps' own map instead: its clusters and the share it counts in bundles (4%+ = SKIP). A map that can't be read means no buy, for the sniper and the desk alike: it looks again later. Several top holders funded by one wallet are one person: 3+ of them holding 4%+, all clusters together 10%+, or 5+ top holders in clusters is a SKIP (DOGGO's ring, OnlyDoge's two hubs); a single small pair costs points. A crowd of brand-new wallets among the top holders (40%+ of the ones traced) is a SKIP too (DOPEUS: no clusters, the coin split across new wallets). Holders paid out by an exchange (a funder with 1000+ transactions) aren't a cluster. Also: top holders RugCheck ties to the insiders (10%+ = SKIP), how much the dev / creator still holds, the dev wallet's history, a bundled launch (many wallets buying in the very first block), **copycats** (an older coin with the same ticker and 3x the liquidity), holders |
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
**AUTO** = buys the BUYs that pass a second look. It starts on AUTO, or on WATCH when the service starts with
`LIVE_MODE=true` (`SNIPE_MODE` sets it). Switching to REAL from the page keeps the fast lane on whatever it's on, and
the REAL confirmation says which: set it to WATCH first if only the desk should buy with real money.

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

**In and out.** The sniper's coins get their own plan: stop at −25%, sell 50% at 2x, 25% at 3x, 12.5% at 4x and 6.25% at 5x (the last 6.25% rides), a 20%
trailing stop once it's been up 30%, **out at −12% if it's still falling 10%+ in 5 minutes in its first 30 minutes**
(the move failed: no riding it down to the stop), and **out if it hasn't made +10% within 20 minutes** (no momentum,
no stay). A loss rule only sells once Jupiter's real quote agrees: a wick on the price feed doesn't sell.
At 12 hours it sells, unless buyers are still ahead and it's still rising: then it rides up to 3x longer. Every
number is a variable (`SNIPE_*` below).

**Why it lost, and what it learns from it.** Every sniper buy keeps a fingerprint of what was true at the buy: the
coin's numbers (market cap, age, the hour's and the last 5 minutes' move, liquidity, the pool's pair) and what the checks
saw and let through: how many top holders were brand-new wallets, a cluster on the bubble map, insiders, the buyers
against the sellers, the top 10's share, its score, and the list it was found on. A coin that closes at a loss gets a
review in TRADES and the live feed that names those warning signs (DOPEUS: "5 of the 16 top holders it traced were
brand-new wallets; the buyers were only 1.3x the sellers; its score was 74; it fell 27% within 4 min of the buy: a fast
dump"). Then it learns. Among the sniper's coins closed in the last 48 hours (15 at most), a sign that lost at least
twice, and more than twice as often as it won, is **avoided from then on** ("AVOIDS NOW" on the page, and a "learned:"
line in the live feed the moment it learns it). A **fast dump** (down 20%+ within 10 minutes of the buy) counts as two
losses for the warning signs, so one is enough to stop buying coins with that sign; plain tags like "under an hour old"
need two real losses. After 48 hours a lesson fades and the sign gets another look. Two rules are fixed: the sniper
doesn't buy a coin down 20%+ in the last hour ($.fun: bought at $81.5K on a bounce, an hour after it topped near
$200K), and the desk doesn't buy one down 25%+. A pool paired with another memecoin costs 8 points.

**Nobody buying (PQC).** PQC was a desk pick bought with nobody buying it, then held through a slow slide all the way
to the -25% stop (-$9.73). Now the desk doesn't buy a coin with fewer than 5 buys in the last 5 minutes
(`DESK_MIN_BUYS_5M`), and a retrace bounce needs those buyers too. Once in, any coin of either bot that's down 3% or
more (`DEAD_LOSS_PCT`) with nobody buying (2 buys or fewer in 5 minutes, `DEAD_MAX_BUYS`, or the sellers 3x the
buyers) and not rising, for a minute (`DEAD_SECONDS`), is sold: out at about -$1 to -$3 instead of riding to the
stop. Buyers coming back resets the minute. The desk's coins also get a never-green rule: 45 minutes in, never green,
at -8%, out (`DESK_RED_MINUTES` / `DESK_RED_PCT`). Desk buys now keep their fingerprint too, so a desk loss gets a review
("only 0 buys in the 5 minutes before it bought: nobody was buying it").

**No chasing a run (CATBOT).** CATBOT ran +508% in 80 minutes, then its creator pulled the money. A coin up 300%+
over the last hour (an hour old or more, `SNIPE_MAX_RUN_H1_PCT`), or 500%+ over the last 6 hours (6 hours old or more,
`SNIPE_MAX_RUN_PCT`), isn't bought by either bot: it waits for the run to settle and looks again. A coin up 150-300%
this hour is a **hot entry**: bought at half the ticket on the fast plan, with nothing left riding (half out at +25%,
the rest at +50%, out at -15%). A coin under an hour old isn't held back by any of this: its "hour" is since launch.

**Already moving, buyers ahead.** The sniper buys a coin an hour old or more only when it's up 15%+ this hour
(`SNIPE_MIN_H1_PCT`), and any coin only when the last 5 minutes' buys are at least 1.5x the sells
(`SNIPE_MIN_BUYERS_X`). In the simulation, every buy under 1.5x was a coin going sideways (26 of them, -$15 between
them) while the movers were bought at 2-3x; the flat ones were in-and-out for a dollar or two, taking the slots the
movers needed.

**The time box: big wins in a short time, long holds only on the way to a 2x.** Past 45 minutes, a sniper coin that
isn't up +25% or more is sold, whatever it's doing (`SNIPE_BOX_MINUTES` / `SNIPE_BOX_GAIN_PCT`). The fast plan's box is
30 minutes and +15%; the desk's, older coins that move slower, 2 hours and +20%. A coin that keeps its gain past the
box rides on with the ladder, the trail and the momentum exits; the last 25% riding after 75% was banked is never
boxed. The trade plan shows a "time box" row.

**How these were tested: three hours, many times over.** `sim/` holds a simulated market: coins born every few minutes,
each of a kind taken from real trades (CRAWLER the runner, PUTBACK the early pop, CATBOT the pump and rug, DOPEUS the
fresh-wallet dump, DOGGO the bundle, PQC the slow bleed, dead coins, chop, slow grinders, clean 2x's), answering every
service the bot reads in their real shapes. The bot runs on its own code against it on a simulated clock
(`python3 sim/run.py --hours 3 --seed 1`; the simulator stays with the tests, it isn't one of the files Railway needs).
It's a model, not the market: it shows how the rules react to each shape, not what the real market will pay.

**The dial: how hard to push today.** A number from 0.4 to 1, read every minute, shown in THE DESK box and the fast
lane's line. It starts at 1 and goes down for:
- the bots' own last 24 hours on the money that's on (4 coins closed at least): under half of them won x0.8, under a quarter x0.6;
- today: down half the daily loss limit, x0.75;
- the market: the median hour of the hot coins the fast lane read in the last 30 minutes, 0 to -10% x0.85, under -10% x0.7.
Every automatic ticket, the desk's and the sniper's, is multiplied by it (your own buys from CHECK A COIN aren't), and
under 0.5 (`DIAL_EARLY_MIN`, was 0.75) there are no early bets that day: "today's dial is 0.60: too weak for early bets".
`DIAL=off` keeps full tickets.

**Brand-new wallets on the bubble map (DOPEUS).** DOPEUS's map had no clusters at all: every holder sat in a pair with
one other wallet, the coin split across wallets made to hold it. The map check now also reads, for each top holder,
whether it's a brand-new wallet (under 100 transactions with the first one in the last 3 days, or one whose very first
transaction gave it the coin). 40%+ of the traced top holders brand-new (5 at least) is a SKIP (`SNIPE_MAP_FRESH_PCT`);
20%+ costs 8 points and takes away the "map clean" points. With `BUBBLEMAPS_API_KEY` set, the chain is still read for
this, next to Bubblemaps' clusters.

**Early bets: $20K to $100K.** The coins that can 10x and the ones that lost the most (PUTBACK: sniped at $43.5K, sold
60% at +48-50% within minutes). The floor is $20K (`SNIPE_MIN_MCAP_USD`); under $100K (`SNIPE_EARLY_MCAP_USD`) it's an
early bet. Extra careful in, against rugs and tops: rising this hour, paired with SOL or USDC, a clean bubble map that
was actually read (no top holders funded by one wallet, no crowd of brand-new wallets), 15+ minutes old
(`SNIPE_EARLY_MIN_AGE_MINUTES`), no 20%+ 5-minute candle (`SNIPE_EARLY_MAX_M5_PCT`), buyers 1.2x the sellers
(`SNIPE_EARLY_BUYERS_X`), insiders under 5% (`SNIPE_EARLY_INSIDER_PCT`), half the ticket (`SNIPE_EARLY_SIZE=0.5`).
Its score bar is the sniper's own 70; `SNIPE_EARLY_BUY_SCORE` raises it. And **profits taken fast**: out at -15%
(`SNIPE_EARLY_STOP_LOSS_PCT`), or -8% while still falling 6%+ in 5 minutes; 40% sold at +25% and 35% more at +50%
(`SNIPE_EARLY_SCALE_OUT=25:40,50:35`); once up 15%, if it turns (15% off its best, back under +5%, or the momentum dies)
it sells down to its last 25% at once. Its spike and turning-down sales never take it under that 25%
(`SNIPE_EARLY_KEEP_PCT`), which rides as the runner: out 30% off its best, or back at break-even, never a loss. The
trade plan shows an "early bet" row. If early bets keep losing, "under $100K market cap" shows up under AVOIDS NOW like
any other lesson.

**What it learned.** Every closed sniper trade keeps the signals it was bought on. The page shows, for each signal,
how many coins, how many won, the average result and the P&L, so you can see which signals actually pay and raise or
lower the bar. It doesn't change its own rules: you do, with the variables.

**Honest limits.** No bot can promise to beat the best traders. Most new memecoins go to zero, and the earlier you
buy, the less there is to check. The sniper can't see private Telegram groups or a celebrity's post before
everyone else. It only reads X, TikTok and Reddit through the web search, which needs `ANTHROPIC_API_KEY` and costs
about $0.01 per search plus tokens (at most 3 searches a check, `BUZZ_PER_HOUR` checks an hour). Run it on paper first.

## What's new (Oct 15): fast in-and-out sniper, learned cash-out, viral hold, speed profiles, certified desk

**1. The sell ladders.** Two plans, one for each kind of sniper coin, and the desk:
- **Fast plan (every sniper coin, the default).** In and out. 25% of the bag is sold at +15%, 25% at +30%, 25% at +50%,
  and the last 25% rides as the runner. Stop-loss −15%, out at −8% if it is still falling, out in 10 minutes if it isn't
  moving (+6% needed), a 20-minute box (+12% needed), 3 hours at most. (`SNIPE_FAST_*`; `SNIPE_FAST=off` returns to the old plan.)
- **Viral plan (hype 100/100 only), and the desk.** 50% of the bag at 2x, 25% at 3x, 12.5% at 4x, 6.25% at 5x
  (`SCALE_OUT` / `SNIPE_SCALE_OUT`, `100:50,200:25,300:12.5,400:6.25`); the last 6.25% is the runner (30% off its best, or
  back at break-even, never a loss).
The protective exits (stop-loss, trailing stop, "move failed", time box) run in both plans. On a $5 ticket a sale that
would leave under about $2.50 sells everything, so the small steps fold into one final sale.

**1b. The learned cash-out line.** A fast-plan coin also has one line at which the **whole remaining bag** is sold. It
starts at +100% (`SNIPE_CASHOUT_PCT`) and the bot re-learns it after each closed fast-plan trade (`learn_exit.py`): over
the last 48 hours of its own trades it asks "had I sold everything the moment the coin touched +40 / 60 / 80 / 100 / 150%,
would I have done better?", and moves the line to the best one, only after 10+ trades and only on a clear win. It never
leaves +40% to +150%. The reason is in the live feed ("learned: ..."). This is a rough guide on a small sample, not a promise:
`SNIPE_LEARN=off` keeps the line fixed. It never touches the stop-loss or the viral coins.

**2. Speed profiles (`SPEED_PROFILE`).** A faster sniper without touching a single safety rule:

| profile | fast-lane look | second look | full checks per round | price reads while holding |
|---|---|---|---|---|
| `normal` | 60 s | 60 s | 2 | 10 s |
| `balanced` (default) | 45 s | 30 s | 2 | 5 s |
| `fast` | 30 s | 30 s | 3 | 5 s |
| `turbo` | 20 s | 15 s | 4 | 5 s |

A profile only fills in variables you haven't set: `SNIPE_SCAN_SECONDS=45` in Railway beats it. Faster means more calls to
GeckoTerminal, DexScreener, RugCheck and your RPC. If the live feed starts showing "too many requests" (429), or your Helius
plan runs out early, go down a step. The code is in `profiles.py`.

**3. Viral-hype hold mode.** Everything else is in and out. The one exception is a sniper coin bought at **hype 100/100**
(`SNIPE_VIRAL_HYPE`) with a narrative behind it, the kind that might go viral: it is held on the 2x-5x ladder with a 3-hour
time box (`SNIPE_VIRAL_BOX_MINUTES`), no stall exit for 60 minutes, up to 24 hours, and a 30% trailing stop. Its stop-loss is
the sniper's normal −25% (a viral coin swings too much for the fast plan's −15%). **Hype 100 is rare**: it takes 40+ wallets
buying in 5 minutes, 600+ new holders an hour, volume at 2x its pace, a big hour and a trending list all at once. Set
`SNIPE_VIRAL_HYPE=90` to let more coins in, `0` to turn the mode off. Hype is read once, at the buy; a coin whose hype dies
is only caught by the price rules.

**3b. More trades.** `MAX_POSITIONS` 5 to 8 (3 desk, 5 sniper), `MAX_EXPOSURE_PCT` 25 to 40, `SNIPE_BUY_SCORE` 70 to 65,
`SNIPE_MIN_BUYERS_X` 1.5 to 1.0 (buys at least equal to sells; early bets `SNIPE_EARLY_BUYERS_X` 1.2 to 1.0, early-bet candle cap `SNIPE_EARLY_MAX_M5_PCT` 20 to 30, the dial an early bet needs `DIAL_EARLY_MIN` 0.75 to 0.5), rest after 3 losses in a row (was 2) for 15 minutes (was 30). **Not changed:** the rug
checks and vetoes, the −15% / −25% stops, the 20% daily loss limit, and the 6% ticket size (`TICKET_SHARE` in `main.py`).
More trades means more fees and more small losses; whether it makes more money is something paper mode has to show first.

**4. The certified desk (off by default).** Before the desk buys its pick, the pick must be **certified** (`certify.py`): every check below
read and passed. An unknown is a fail, never a pass.
- mint and freeze authority confirmed revoked, no Token-2022 trap, RugCheck's full report read and not rugged
- sniper score 75+ (`DESK_CERT_SCORE`), pool liquidity $50K+, 6+ hours old, 500+ holders
- biggest wallet 5% or less, top 10 35% or less, dev 3% or less, insiders 3% or less
- liquidity 50%+ locked or burned (a pool $250K deep and a day old may pass with no lock: many older pools have none)
- bubble map read: funded clusters 2% or less, brand-new wallets 20% or less of the traced top holders

It's much stricter than `DESK_GATE_SCORE=65`, and when a data source is rate-limited the numbers come back unread and
every coin fails. In practice it can stop the desk buying at all, so it ships **off** (`DESK_CERTIFIED=on` to use it). A certified coin gets the desk's longer-hold
plan: time box 4 hours (was 2), never-green exit at 90 minutes (was 45), max hold 48 hours (was 24), trailing stop 20% off the
best (was 15%): `DESK_CERT_BOX_MINUTES`, `DESK_CERT_RED_MINUTES`, `DESK_CERT_HOLD_HOURS`, `DESK_CERT_TRAIL_PCT`. The stop-loss
stays the same. Every number in the list has a `DESK_CERT_*` variable (see `certify.py`); `DESK_CERTIFIED=off` goes back to
the old gate. **"Certified" means "passed these checks", not "safe".** Rug checks lower the odds of buying a rug; they can't
see a dev who sells from a fresh wallet an hour later, a coin that just goes down, or anything the data sources get wrong.

## Why isn't it trading? (`/api/status`)

Open `https://YOUR-APP.up.railway.app/api/status` and look at **`why_not_trading`**: over the last 12 hours, how many coins
the sniper checked, how many passed, and the top reasons the rest were turned down (numbers blanked, so "top 10 wallets hold
52%" and "...48%" count together). It tells you which rule is doing the blocking:
- `geckoterminal is rate-limiting` / `bubble map couldn't be read` / `unknown`: the data feeds are the problem, not your rules.
  Set `SPEED_PROFILE=normal`.
- `mint authority`, `freeze`, `top # wallets`, `lp`, `cluster`: the rug checks. Those are doing their job; they are the last
  thing to loosen.
- `buyers aren't ahead`, `just ran`, `spiking`, `score ... is under`: the entry timing rules. These are the ones to ease
  (`SNIPE_MIN_BUYERS_X`, `SNIPE_MAX_M5_PCT`, `SNIPE_BUY_SCORE`).
- `not certified`: that gate is on (`DESK_CERTIFIED=on`). Turn it off.
- `learned from its own trades`: the bot's own lessons are blocking it. A trait of the coin ("under an hour old", "thin
  liquidity") is avoided for 24 hours (`SNIPE_LESSON_HOURS`) once coins with it lost 4 times (`SNIPE_LESSON_LOSSES`; a fast
  dump counts twice), lost more than twice as often as they won, **and lost money overall**. "Scored under 80" is never a
  lesson. (It used to be 2 losses and 48 hours, judged by head count, and could ban nearly every kind of coin.)
  `SNIPE_LESSONS=off` never blocks on them. They are a record of real losses, so turning them off means repeating those buys.
- `not moving: +3% this hour`: `SNIPE_MIN_H1_PCT` (now 5, was 15). A coin that stays flat is also sold by the fast plan's
  10-minute stall exit.
`desk_benched` in the same page is the **desk's** side: the coins its own filters and its AI judge ("Jev") turned down and are
still benched, by reason. `liquidity_fits_ticket` there means the judge said the ticket couldn't be sold into the pool; the
judge is now asked about the ticket the bot would really buy (cut to 2% of the pool), not the uncut 6% of the bank. If one
reason dominates, that rule is your blocker (`SOFT_LIQUIDITY_FITS_TICKET`, now 0.40, was 0.60, is the bar for that one; `PRACTICE_MODE=true`, paper
only, opens the desk's gates so you can watch the whole loop).
The live feed also lists each stop in plain words ("desk pick stopped by the sniper's checks: ...").

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

They split the slots so one can never crowd out the other: with `MAX_POSITIONS=8`, 3 are the desk's and 5 the
sniper's (`DESK_SLOTS`, `SNIPE_SLOTS`). A coin you paste and tell it to buy may take any free slot. The POSITIONS
panel says which bot bought each coin, and the bottom section has a box for each bot saying what it's doing.

If you want the desk trading as much as it did the first night, `PRACTICE_MODE=true` opens its gates again (paper
only); the safety checks now run on every pick either way.

## Several coins at once

The desk holds up to `MAX_POSITIONS` coins (default **8**) and keeps scanning while it has a free slot. Each coin
gets its own plan below. All coins together stay under `MAX_EXPOSURE_PCT` (default **40**%) of everything (cash +
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
| stop-loss | `STOP_LOSS_PCT=25` | down 25% from the buy → sell everything (the sniper's coins: `SNIPE_STOP_LOSS_PCT=25`) |
| move failed | `DESK_FAIL_PCT=15`, `DESK_FAIL_M5_PCT=10`, `DESK_FAIL_MINUTES=60` | in its first hour, down 15% and still falling 10%+ in 5 minutes → out before the stop (the sniper's line: −12% in 30 minutes) |
| never went green | `SNIPE_RED_MINUTES=10`, `SNIPE_RED_PCT=5` | the sniper's coins: 10 minutes in, never above +2% and now down 5% → out early |
| take-profit (all) | `TAKE_PROFIT_PCT=0` (off) | up X% → sell everything |
| max hold | `MAX_HOLD_HOURS=24` | held this long → sell everything |
| **momentum died** | `MOMENTUM_SELL_PCT=75`, `FADE_OFF_PEAK_PCT=4`, `FADE_VOLUME_PACE=0.6` | once it has been up 20% (the sniper's coins: 30%): it slips 4%+ off its best, sellers are ahead in the last 5 minutes and the volume is drying up (the last 5 minutes at 0.6x the hour's pace or less) → **75% out, the last 25% rides** |
| profit lock | `TRAIL_LOCK_PCT=8` | once it has been up 20%, it never lets it fall back under +8%: 75% out there, the rest rides |
| trailing stop | `TRAIL_ARM_PCT=20`, `TRAIL_PCT=15` | once it has been up 20%, a 15% drop from its best price: 75% out, the rest rides |
| the runner | `RUNNER_TRAIL_PCT=30`, `RUNNER_FLOOR_PCT=0` | the last 25% rides with no ladder and no trims: out when it's 30% off its best, or back at break-even (it never turns into a loss). `MOMENTUM_SELL_PCT=100` sells everything at once instead |
| profit ladder | `SCALE_OUT=100:50,200:25,300:12.5,400:6.25` | at 2x sell 50% of the original bag, at 3x 25%, at 4x 12.5%, at 5x 6.25%; the last 6.25% is the runner (below) |
| breaking down | `WATCH_DROP_M5_PCT=8` | in a loss and down 16%+ in 5 minutes with sellers 1.5× buyers → sell everything |
| turning down | `WATCH_DROP_M5_PCT=8`, `WATCH_DERISK_PCT=50` | in profit, down 8%+ in 5 minutes with sellers 1.3× buyers → once it has been up 20%: 75% out, the rest rides; before that, half of what's left (at most every 10 minutes) |
| spike | `WATCH_PUMP_M5_PCT=20`, `WATCH_PUMP_SELL_PCT=30` | up 20%+ in 5 minutes while 20%+ in profit → take 30% off (at most every 15 minutes) |

RISK's volume rule still runs every 5 minutes on top: volume ratio under 0.20 on **two checks in a row** sells
everything. It only applies to a pool more than 2 hours old and 20 minutes after the buy (five minutes of volume is
noise, and a pump.fun coin moving to its own pool pauses trading for a few minutes).

**Never sold at a bad price.** Before any sale, the quote is compared with the market price. A quote under half of
what the market prices the coins at, while the pool still has money in it, is a routing hiccup (a coin moving to its
pool, a route that isn't ready): it doesn't sell, and tries again on the next check. A real rug (the pool emptied)
still sells for whatever there is, and after 20 minutes of bad quotes it sells anyway.

**Every desk pick gets the sniper's checks, and its standard.** Before the desk buys its own pick, the sniper runs its
full check: RugCheck, locked liquidity, the dev wallet, bundles, copycats, whether people can sell, buyers against
sellers right now. The night of Oct 13-14 the desk's picks were most of the losses (Catnip, Altai, Buddy, DOPE,
ARCADES, ROSE): scores of 42-61 against a bar of 25, buyers only 1.0-1.2x the sellers, micro caps on $26-28K pools,
bought right after a +17-28% candle. So now a desk pick isn't bought when:
- the check is a SKIP, the bubble map couldn't be read, or the coin just ran (300%+ in an hour);
- one of the lessons below says so (both bots learn from both bots' trades);
- nobody's buying (under 5 buys in 5 minutes), or the buyers aren't at least 1.5x the sellers (`DESK_MIN_BUYERS_X`);
- it's a micro cap under $150K (`DESK_MIN_MCAP_USD`): those are the sniper's early bets, with their stricter checks;
- it's spiking 15%+ in 5 minutes (`DESK_MAX_M5_PCT`), falling 25%+ this hour, or dumping right now;
- its score is under 65 (`DESK_GATE_SCORE`, the sniper's own bar is 70).
The retrace entry asks the same of the bounce: buyers 1.5x the sellers, and a calm green candle, not a 15%+ one. After
2 desk losses in a row the desk rests for an hour (`DESK_LOSS_STREAK` / `DESK_COOLDOWN_MINUTES`), shown in THE DESK
box. When the checks stop a pick, that coin sits out an hour and the desk scans again a minute later (not five).
`DESK_GATE=off` turns the checks off.

**The desk waits for its entry.** A pick that passes isn't bought on the spot (PQC: bought at $258K while it slid from
$300K, and sat −22%). It waits up to 45 minutes (`DESK_RETRACE_MINUTES`) and holds its slot meanwhile, then buys one of two ways:
- **the retrace:** 8%+ under its high since the pick (`DESK_RETRACE_PCT`), then bouncing (green over 5 minutes, buyers
  at least even with sellers) and still up over 6 hours. A bounce in a coin down over 6 hours is not an entry;
- **the breakout:** no pullback, but momentum shifts up hard: up 10%+ since the pick (`DESK_BREAKOUT_PCT`) and at its
  high, +3% to +30% over 5 minutes (a 30%+ candle is the top, not an entry), buyers 1.5x the sellers (15 buys at least),
  5-minute volume 1.5x the hour's pace, and still up this hour, on two checks in a row (20 seconds apart), so one wick
  doesn't buy.
Down 20% since the pick (`DESK_RETRACE_MAX_DROP`), it's dropped: it kept falling. After 45 minutes with neither, it's
dropped too: it doesn't chase a slow drift up. A dropped pick sits out an hour and the desk scans again a minute later.
`DESK_RETRACE_PCT=0` buys at once, as before; `DESK_BREAKOUT_PCT=0` turns the breakout off.

**Why it bought.** Every position keeps the bot's reasons: Jev's pick (how worth trading, how sure), the safety-check
score and its best points, the 5-minute, 1-hour and 6-hour moves at the pick, and how it got in (the retrace or the
breakout, with the numbers). The sniper's buys keep their score and best signals. They're in the trade plan under
"why it bought".

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
| `LIVE_MODE` | `false` | exactly `true` or `false`: the money it starts on, until you pick FAKE or REAL on the page |
| `BOT_WALLET_KEY` | none | real money only. The bot account's private key, in Phantom's export format. Railway only, never the page |
| `OWNER_WALLET` | none | your own wallet's address: the only place withdrawals go. Set from the page by connecting Phantom; this variable overrides it |
| `REAL_MAX_TICKET_USD` | `25` | on real money, no single buy over this |
| `JUPITER_API_KEY` | none | recommended |
| `SOLANA_RPC_URL` | public endpoints | strongly recommended: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY` (free key at helius.dev; the `beta.helius-rpc.com` gateway is a public beta and refuses heavy holder lookups more often). Public endpoints often refuse the holder lookup; then the top holders come from RugCheck's list instead, and if that fails too the coin is cut as `top_wallet_unknown`. |
| `RPC_MAX_PER_SEC` | `8` | Solana RPC calls a second at most, all together (the bubble map reads many wallets; free plans limit calls a second) |
| `GOAL_USD` | none | a starting goal for the top bar (a goal set from the page wins) |
| `FOMO_LINK` | none | FOMO's link for one coin with `{mint}` for the address, e.g. copied from FOMO's share button; must start with `https://` |
| `PAPER_BANK_USD` | `1000` | paper mode's starting cash |
| `MIN_TICKET_USD` | `5` | no buy smaller than this |
| `MAX_IMPACT_PCT` | `3` | refuse fills with more price impact |
| `MAX_DAILY_LOSS_PCT` | `20` | pause the desk after losing this % of the day's opening bank |
| `STOP_LOSS_PCT` | `25` | sell everything if the position is down this much (`0` = off) |
| `FEE_RESERVE_SOL` | `0.02` | SOL the bot never spends |
| `RISK_POLL_SECONDS` | `300` | how often RISK checks an open position (at least 30) |
| `CYCLE_SECONDS` | `300` | time between scans (5 minutes) |
| `DESK_TRENDING` | `1h,6h,24h` | the trending windows the desk reads (any of 5m, 1h, 6h, 24h) |
| `TRENDING_PAGES` | `1` | pages of each trending window (0 to 3) |
| `DESK_NEW_POOL_PAGES` | `0` | pages of brand-new pools the desk also reads (0 to 3; new coins are the sniper's) |
| `DESK_METAS` | `60` | coins from DexScreener's trending narratives the desk reads too (0 = none) |
| `TRENDING_WINDOW` | `1h` | what "trending" means: `5m`, `1h`, `6h` or `24h` |
| `PAUSED` | none | `true` starts the desk paused |
| `MAX_POSITIONS` | `8` | coins held at once (1 to 10) |
| `MAX_EXPOSURE_PCT` | `40` | all coins together, as % of cash + coins |
| `TEST_SWAP_USD` | `1` | size of the Test swap round trip, $0.50 to $20 |
| `DESK_TITLE` / `DESK_TAGLINE` | `GROKBOT + JEV` / `JEV PICKS, CODE TRADES` | the page's name and tagline |
| `HARD_<NAME>` | thresholds.py | override one HARD number, e.g. `HARD_MAX_AGE_HOURS=168` |
| `SOFT_<NAME>` | thresholds.py | override one SOFT limit, e.g. `SOFT_MOMENTUM_ALREADY_SPENT=0.75` |
| `SHAPE_MIN_CROWD`, `PICK_MIN_WORTH`, `PICK_MIN_CONF` | thresholds.py | override those three |
| `WATCH_SECONDS` | `10` | how often WATCH reads the price of each coin held (5 at least) |
| `TRAIL_LOCK_PCT` / `SNIPE_TRAIL_LOCK_PCT` | `8` / `10` | once the trail is armed, sell rather than fall back under this gain |
| `SPEED_PROFILE` | `balanced` | `normal`, `balanced`, `fast` or `turbo`: how often the sniper looks and WATCH reads prices (see "What's new") |
| `SCALE_OUT` | `100:50,200:25,300:12.5,400:6.25` | the desk's profit ladder: gain% : % of the original bag (`off` = none) |
| `SNIPE_SCALE_OUT` | `100:50,200:25,300:12.5,400:6.25` | the sniper's profit ladder |
| `SNIPE_LESSONS` / `SNIPE_LESSON_LOSSES` / `SNIPE_LESSON_HOURS` | `on` / `4` / `24` | the sniper avoids a coin trait after this many losses in this many hours (`off` = never blocks on lessons) |
| `SNIPE_VIRAL_HYPE` | `100` | hype (0-100) at the buy that puts a sniper coin in hold mode (`0` = off) |
| `SNIPE_FAST` | `on` | every non-viral sniper coin uses the fast in-and-out plan |
| `SNIPE_FAST_SCALE_OUT` / `SNIPE_FAST_KEEP_PCT` | `15:25,30:25,50:25` / `25` | the fast plan's profit steps and the share kept as the runner |
| `SNIPE_FAST_BOX_MINUTES` / `SNIPE_FAST_BOX_GAIN_PCT` / `SNIPE_FAST_STALL_MINUTES` / `SNIPE_FAST_STALL_GAIN_PCT` / `SNIPE_FAST_MAX_HOLD_HOURS` | `20` / `12` / `10` / `6` / `3` | the fast plan's time box, no-momentum exit and max hold |
| `SNIPE_CASHOUT_PCT` / `SNIPE_LEARN` / `SNIPE_LEARN_MIN_TRADES` | `100` / `on` / `10` | the start of the whole-bag cash-out line, whether it is re-learned, and the trades needed before it learns |
| `SNIPE_VIRAL_BOX_MINUTES` / `SNIPE_VIRAL_STALL_MINUTES` / `SNIPE_VIRAL_MAX_HOLD_HOURS` / `SNIPE_VIRAL_TRAIL_PCT` | `180` / `60` / `24` / `30` | the viral coin's time box, no-momentum exit, max hold and trailing stop |
| `DESK_CERTIFIED` | `off` | `on` = the desk only buys a certified pick (strict: an unreadable number is a fail) and holds it longer |
| `DESK_CERT_BOX_MINUTES` / `DESK_CERT_RED_MINUTES` / `DESK_CERT_HOLD_HOURS` / `DESK_CERT_TRAIL_PCT` | `240` / `90` / `48` / `20` | a certified coin's time box, never-green exit, max hold and trailing stop |
| `DESK_CERT_SCORE` | `75` | the sniper score a certified pick needs |
| `DESK_CERT_MIN_LIQ_USD` / `DESK_CERT_MIN_AGE_HOURS` / `DESK_CERT_MIN_HOLDERS` | `50000` / `6` / `500` | pool liquidity, age and holder count a certified pick needs |
| `DESK_CERT_MAX_TOP_WALLET_PCT` / `DESK_CERT_MAX_TOP10_PCT` / `DESK_CERT_MAX_DEV_PCT` / `DESK_CERT_MAX_INSIDER_PCT` | `5` / `35` / `3` / `3` | holder-concentration limits |
| `DESK_CERT_MAX_CLUSTER_PCT` / `DESK_CERT_MAX_FRESH_PCT` / `DESK_CERT_MIN_LP_PCT` | `2` / `20` / `50` | bubble-map and liquidity-lock limits |
| `DESK_GATE` / `DESK_GATE_SCORE` | `on` / `65` | every desk pick must pass the sniper's safety checks, not be dumping, and score at least this |
| `DESK_MIN_BUYERS_X` / `DESK_MIN_MCAP_USD` / `DESK_MAX_M5_PCT` | `1.5` / `150000` / `15` | a desk pick needs buyers this many times the sellers, this market cap, and no 5-min candle this big |
| `DESK_LOSS_STREAK` / `DESK_COOLDOWN_MINUTES` | `2` / `60` | after this many desk losses in a row, the desk rests this long |
| `DESK_RETRACE_PCT` / `DESK_RETRACE_MINUTES` / `DESK_RETRACE_MAX_DROP` | `8` / `45` / `20` | a desk pick waits for a pullback this deep and a bounce, this long at most, and is dropped if it falls this far (`0` = buy at once) |
| `DESK_BREAKOUT_PCT` | `10` | or buys a breakout: up this much since the pick with the buyers and volume heavily ahead (`0` = off) |
| `DESK_SLOTS` / `SNIPE_SLOTS` | 40% / 60% of `MAX_POSITIONS` (3 / 5) | the slots each bot may fill |
| `SNIPE_MODE` | `auto`, `watch` with `LIVE_MODE=true` | the fast lane: `off`, `watch` (never buys) or `auto`; the page's buttons change it |
| `SNIPE_BUY_SCORE` | `65` | the score a BUY needs |
| `SNIPE_SCAN_SECONDS` | `60` | how often the fast lane looks |
| `SNIPE_MIN_AGE_MINUTES` / `SNIPE_MAX_AGE_HOURS` | `10` / `72` | how young and how old a coin it looks at |
| `SNIPE_CONFIRM_SECONDS` / `SNIPE_CONFIRM_DROP_PCT` | `60` / `4` | the second look before a fast-lane buy, and how far the price may slip in it (`0` = buy at once) |
| `SNIPE_MAX_M5_PCT` | `30` | up this much in 5 minutes = the top of a candle: wait (desk picks too) |
| `SNIPE_LOSS_STREAK` / `SNIPE_COOLDOWN_MINUTES` | `3` / `15` | after this many sniper losses in a row, no fast-lane buys for this long |
| `SNIPE_FAIL_PCT` / `SNIPE_FAIL_M5_PCT` / `SNIPE_FAIL_MINUTES` | `12` / `10` / `30` | the move failed: out at −12% while still falling 10% in 5 min, in the first 30 min |
| `SNIPE_MIN_LIQUIDITY_USD` | `8000` | thinner than this is too hard to get out of |
| `SNIPE_MIN_MCAP_USD` / `SNIPE_MAX_MCAP_USD` | `20000` / `30000000` | the market cap window (`100000` turns early bets off) |
| `SNIPE_EARLY_MCAP_USD` / `SNIPE_EARLY_SIZE` | `100000` / `0.5` | under this market cap is an early bet, at this share of the ticket |
| `SNIPE_EARLY_MIN_AGE_MINUTES` / `SNIPE_EARLY_MAX_M5_PCT` / `SNIPE_EARLY_BUYERS_X` / `SNIPE_EARLY_INSIDER_PCT` / `SNIPE_EARLY_BUY_SCORE` | `15` / `30` / `1.0` / `5` / `70` | an early bet's extra checks: this old, no 5-min candle this big, buyers this many times the sellers, insiders under this, this score |
| `SNIPE_EARLY_SCALE_OUT` / `SNIPE_EARLY_KEEP_PCT` | `25:40,50:35` / `25` | an early bet's profit steps (gain%:sell% of the bag) and the share that always rides |
| `SNIPE_EARLY_STOP_LOSS_PCT` / `SNIPE_EARLY_FAIL_PCT` / `SNIPE_EARLY_FAIL_M5_PCT` / `SNIPE_EARLY_RED_PCT` | `15` / `8` / `6` / `4` | an early bet's loss lines: the stop, the failed move (while still falling this much in 5 min), never green |
| `SNIPE_EARLY_TRAIL_ARM_PCT` / `SNIPE_EARLY_TRAIL_PCT` / `SNIPE_EARLY_LOCK_PCT` | `15` / `15` / `5` | once up this much, it sells down to its last 25% when it falls this far off its best or back under this gain |
| `SNIPE_MAX_TOP_WALLET_PCT` / `SNIPE_MAX_TOP10_PCT` / `SNIPE_MAX_DEV_PCT` | `8` / `45` / `10` | rug lines |
| `SNIPE_BUNDLE_WALLETS` | `8` | this many wallets in the launch block = a bundled launch |
| `SNIPE_STOP_LOSS_PCT`, `SNIPE_SCALE_OUT`, `SNIPE_TRAIL_ARM_PCT`, `SNIPE_TRAIL_PCT` | `25`, `100:50,200:25,300:12.5,400:6.25`, `30`, `20` | the sniper's exits (sells at 2x, 3x, 4x and 5x) |
| `SNIPE_STALL_MINUTES` / `SNIPE_STALL_GAIN_PCT` | `20` / `10` | out if it hasn't made +10% within 20 min |
| `SNIPE_MAX_HOLD_HOURS` / `SNIPE_RIDE_X` | `12` / `3` | max hold, and how much longer it rides while buyers lead |
| `SNIPE_NEED_NARRATIVE` / `SNIPE_MIN_HYPE` | `on` / `50` | buy only coins with a narrative, and hype at least this |
| `SNIPE_MIN_UPSIDE_X` | `2` | buy only with room for at least this many x |
| `BUBBLEMAPS_API_KEY` | none | Bubblemaps' own map (key from pro.bubblemaps.io, a paid plan; 25 of its credits a coin, read at most every 10 minutes) |
| `SNIPE_MAP` | `on` | the bubble map check (who funded the top holders); read for desk picks, your own checks and coins close to a BUY |
| `SNIPE_MAP_CLUSTER_PCT` / `SNIPE_MAP_TOTAL_PCT` / `SNIPE_MAP_MAX_WALLETS` | `4` / `10` / `5` | SKIP when 3+ holders of one funder hold this %, all clusters hold this %, or this many top holders are in clusters |
| `DESK_MIN_BUYS_5M` | `5` | a desk pick with fewer buys than this in the last 5 minutes isn't bought |
| `DEAD_LOSS_PCT` / `DEAD_MAX_BUYS` / `DEAD_SECONDS` | `3` / `2` / `60` | down this much with this few buys in 5 min (or the sellers 3x the buyers), not rising, this long: out (`0` = off) |
| `DESK_RED_MINUTES` / `DESK_RED_PCT` | `45` / `8` | a desk coin that never went green this long and is down this much: out |
| `SNIPE_MAX_RUN_H1_PCT` / `SNIPE_HOT_H1_PCT` | `300` / `150` | up this much in the last hour: wait; up between the two: a hot entry, half the ticket on the fast plan with nothing left riding (`SNIPE_HOT_SCALE_OUT=25:50,50:50`) |
| `SNIPE_MIN_H1_PCT` | `5` | a coin an hour old or more is bought by the sniper only if it's up this much this hour (`0` = off) |
| `SNIPE_MIN_BUYERS_X` | `1.0` | the sniper buys only when the last 5 minutes' buys are at least this many times the sells (`0` = off) |
| `SNIPE_BOX_MINUTES` / `SNIPE_BOX_GAIN_PCT` | `45` / `25` | the time box: past this, a sniper coin that isn't up this much is sold |
| `DESK_BOX_MINUTES` / `DESK_BOX_GAIN_PCT` | `120` / `20` | the desk's time box |
| `FAST_BOX_MINUTES` / `FAST_BOX_GAIN_PCT` | `30` / `15` | the fast plan's (early bets, hot entries) time box |
| `SNIPE_MAX_RUN_PCT` | `500` | up this much over the last hour (or 6 hours) = a run nobody chases: both bots wait (`0` = off) |
| `DIAL` / `DIAL_MIN` / `DIAL_EARLY_MIN` | `on` / `0.4` / `0.5` | today's dial: every automatic ticket times it, never under this, and no early bets under this |
| `SNIPE_MAP_FRESH_PCT` / `SNIPE_MAP_FRESH_DAYS` | `40` / `3` | SKIP when this share of the traced top holders are brand-new wallets (first transaction within this many days); `0` = off |
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
- Up to 8 coins at once (`MAX_POSITIONS`).
- The dashboard link is public, but read-only. Anyone with it can see the bot wallet's address and trades. They
  can't pause it or move funds.
- **FOMO:** it has no public trading API, and its terms forbid bots, so this desk trades through Jupiter instead.
  The tokens are the same on-chain Solana tokens.

## Running the tests (on a computer)

```
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
The tests in `tests/test_new_features.py` need no network: they replay a coin from 1x to 5x through the real exit code and check
the sales are exactly 50% / 25% / 12.5% / 6.25%, and cover the speed profiles, the viral-hold plan and the certified check.
They are not needed on Railway; skip the `tests` folder when uploading.
