# mh-sentiment

Collects retail long/short % for XAUUSD from two sources and publishes `sentiment.json`,
which the MH FOREX dashboard reads (same idea as `mh-cot`).

## Setup (one time)
1. Create a **public** GitHub repo named `mh-sentiment` and upload everything in this folder
   (keep the `.github/workflows/sentiment.yml` path exactly).
2. Repo > Settings > Secrets and variables > Actions > **New repository secret**:
   `MFX_EMAIL` and `MFX_PASSWORD` (your Myfxbook login). Secrets never appear in the repo or the EA.
3. Repo > Settings > Actions > General > Workflow permissions > **Read and write permissions**.
4. Repo > Actions > **sentiment** > **Run workflow**. A `sentiment.json` file should appear.
5. In MT5: Tools > Options > Expert Advisors > Allow WebRequest > add
   `https://raw.githubusercontent.com` (already needed for COT).

## Notes
- Runs every 20 minutes (72 Myfxbook calls/day; the free limit is 100).
- The file is rewritten only when a value changes, or once an hour as a heartbeat.
- If one source fails, its last value is reused for up to 3 hours, then dropped.
- `python -m unittest -v` runs the offline tests.
