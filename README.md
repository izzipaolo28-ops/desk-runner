# Read-only pump.fun token scanner

This project only reads public token data, queries Helius RPC, writes `tokens.csv`, and sends Telegram research alerts. It contains **no wallet, keypair, signing, transaction submission, buy, or sell code**.

## Install
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```
Fill in `HELIUS_RPC_URL` and Telegram variables in `.env`. Never commit `.env`.

## Tests first
```bash
python -m unittest discover -s tests -v
```

## Run
```bash
python scanner.py
```

## Filter rules
- top-10 holder wallets must be **strictly below 25%**
- creator/dev wallet must be **strictly below 5%**
- block-0 buyers must be **3 or fewer**
- curve progress must be between **15% and 80%, inclusive**
- missing or uncertain metrics fail closed and are logged

## Important limitations
- The public pump.fun frontend endpoint can change, rate-limit, or be unavailable. Set `PUMP_API_URL` if you use a maintained public feed.
- `getTokenLargestAccounts` returns token accounts, not a complete holder index. This script aggregates the largest returned accounts by owner, but its top-10 value is an estimate and can miss wallets outside the largest-account sample.
- “Block-0 buyers” is provider/transaction-format dependent. The code fails closed if it cannot identify signers. Verify this metric against a trusted indexer before relying on it.
- Token metadata fields (including curve progress and market cap) vary by API version. Missing curve progress causes a rejection.
- This is a research filter, not a prediction of profitability or a safety guarantee. Passing tokens can still be scams or lose all value.
- For a safer bot workflow, use these alerts for manual review and keep the existing trading system in paper mode. This scanner does not tune or activate live trading.


## Risk score and flag report
`risk_score.py` adds a transparent 0–10 heuristic with red flags, green flags, unknown signals, and a conservative verdict. It is for research only, not a buy signal. The example pattern supplied (7 block-0 buyers with identical amounts, top-10 at 38%, a 2-day-old X account with 14 followers, curve at 71%, steady buys, and dev at 2.1% with no sells) scores **3/10 — SKIP**. The live scanner does not independently verify identical buy amounts, X account age/followers, steady buy flow, or developer sell history; these remain unknown and are not assumed positive. CSV and Telegram alerts include score and unknown signals. A score is not a safety guarantee; keep the scanner read-only.

\n## Additional user risk rules (research-only)
- Top 10 holders must be at or below 25%, excluding bonding curve and LP accounts; the exclusion must be verified from source data.
- Developer holding must be at or below 5%.
- A developer sale within the first 10 minutes is a hard-rule failure.
- Tokens older than 6 hours without a new ATH are a hard-rule failure.
- Quebec quiet hours use `America/Toronto`: 01:00–08:59. `trade_window_status()` reports the time-window status.
- User's risk policy is documented: maximum planned exposure 2% of account per trade, maximum 5 open positions, take 50% off at 2x, and stop at -35%. This package remains read-only and does not execute/enforce orders or position limits.
- Reports use SCORE, RED FLAGS, GREEN FLAGS. Unknown metrics are listed as UNVERIFIED red flags, not assumed safe.

Implementation limits: the scanner does not yet independently fetch validated ATH history, age for every token, early developer sells, social-account age/followers, or all data needed to verify curve/LP exclusion from holder concentration. Such signals remain unknown. No wallet, signing, trade execution, or order management is included.
