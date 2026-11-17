# 🧠 brain — the self-updating neural network

Runs every day at 7:00 AM (Florida) on GitHub Actions ([`.github/workflows/brain-daily.yml`](../.github/workflows/brain-daily.yml)), even with your PC off.

Each run:
1. **Collects** the community penny lists (PennyRecon, Paydirt, Penny Pinchin' Mom) → `state/brain.db`
2. **Snapshots** every active item (reports, states, sources, price, age)
3. **Reads your scans** sent to the Telegram bot: `/penny SKU tienda` or `/no SKU tienda`
4. **Retrains** the network (`brain.py`, scikit-learn MLP) once there are ≥15 days of history.
   Question it learns: *"will this item be reported in Florida within 14 days?"*
5. **Writes** `reports/YYYY-MM-DD.md`, `reports/LATEST.md` and `predictions.csv`
6. **Sends** the summary to Telegram and **commits** everything back to this repo

Every day's commit is the brain's progress: open `reports/LATEST.md` to see where it is.

| File | What |
|---|---|
| `brain.py` | the network: snapshot, labels, training, prediction |
| `community.py` | community list scrapers |
| `run.py` | the daily pipeline |
| `state/` | database, model, last training report (updated by the bot) |
| `reports/` | daily analysis reports (updated by the bot) |
| `predictions.csv` | today's ranking, for other parts of the project to read |

Run it by hand: Actions tab → **Brain daily** → **Run workflow**, or locally `python -m brain.run --no-send`.

Needs repo secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` (Settings → Secrets and variables → Actions).
