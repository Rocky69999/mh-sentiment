#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MH FOREX - Retail Sentiment Collector
Copyright 2026, mhforex | Contact: t.me/mhforexlivegroup

Collects retail long/short percentages for one symbol (default XAUUSD) from two
independent sources and writes them to sentiment.json. The MT5 dashboard reads
that file over HTTPS (raw.githubusercontent.com), exactly like the COT feed, so
buyers of the product never need a login or an API key.

  Source "mfx": Myfxbook community outlook (official API; needs the repository
                secrets MFX_EMAIL and MFX_PASSWORD)
  Source "fsd": forexsentimentdata.com overview page (public page, no login)

Output keys (flat on purpose, so the MQL5 side can parse them with a simple
key lookup). A key is omitted when the source has no usable value:

  symbol, updated, updated_ts,
  mfx_long, mfx_short, mfx_ts,
  fsd_long, fsd_short, fsd_ts

*_ts values are UNIX seconds (UTC) of the last successful read of that source.
"""
from __future__ import annotations

import html as htmllib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Tuple
from urllib.parse import quote, quote_plus

import requests

SYMBOL = os.environ.get("SENT_SYMBOL", "XAUUSD").upper()
OUT_FILE = os.environ.get("SENT_OUT", "sentiment.json")
FSD_URL = os.environ.get("FSD_URL", "https://forexsentimentdata.com/")

HTTP_TIMEOUT = 20            # seconds per request
HEARTBEAT_SEC = 55 * 60      # rewrite the file at least this often, even if nothing changed
MAX_CARRY_SEC = 3 * 3600     # reuse the last good value for at most this long if a source fails
USER_AGENT = "MHForexSentimentBot/1.0 (+https://t.me/mhforexlivegroup)"

Pair = Tuple[float, float]
Fetcher = Callable[[str], Pair]

# Anything in here is masked before text reaches the (possibly public) Actions log.
_SECRETS: List[str] = []


def log(message: str) -> None:
    print(message, flush=True)


def scrub(text: str) -> str:
    """Mask credentials and session ids so they can never leak into logs."""
    out = str(text)
    for secret in _SECRETS + [os.environ.get("MFX_PASSWORD", ""), os.environ.get("MFX_EMAIL", "")]:
        if not secret:
            continue
        for variant in {secret, quote(secret), quote_plus(secret)}:
            out = out.replace(variant, "***")
    return out


def valid_pair(long_pct: float, short_pct: float) -> bool:
    """Both values must be percentages and add up to ~100 (sources round, so allow slack)."""
    return (0.0 <= long_pct <= 100.0 and 0.0 <= short_pct <= 100.0
            and abs(long_pct + short_pct - 100.0) <= 2.5)


# --------------------------------------------------------------------------
# Source 1: Myfxbook
# --------------------------------------------------------------------------
def fetch_myfxbook(symbol: str) -> Pair:
    email = os.environ.get("MFX_EMAIL", "")
    password = os.environ.get("MFX_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("MFX_EMAIL / MFX_PASSWORD secrets are not set")

    base = "https://www.myfxbook.com/api"
    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT

    login = http.get(f"{base}/login.json", params={"email": email, "password": password},
                     timeout=HTTP_TIMEOUT)
    login.raise_for_status()
    body = login.json()
    if body.get("error"):
        raise RuntimeError(f"login rejected: {body.get('message')}")
    session = str(body.get("session", ""))
    if not session:
        raise RuntimeError("login returned no session")
    _SECRETS.append(session)

    try:
        reply = http.get(f"{base}/get-community-outlook.json", params={"session": session},
                         timeout=HTTP_TIMEOUT)
        reply.raise_for_status()
        data = reply.json()
        if data.get("error"):
            raise RuntimeError(f"outlook error: {data.get('message')}")
        for item in data.get("symbols", []):
            if str(item.get("name", "")).upper() == symbol:
                long_pct = float(item["longPercentage"])
                short_pct = float(item["shortPercentage"])
                if not valid_pair(long_pct, short_pct):
                    raise ValueError(f"implausible values {long_pct}/{short_pct}")
                return long_pct, short_pct
        raise RuntimeError(f"symbol {symbol} not present in outlook")
    finally:
        try:  # always release the session; failure here is harmless
            http.get(f"{base}/logout.json", params={"session": session}, timeout=10)
        except requests.RequestException:
            pass


# --------------------------------------------------------------------------
# Source 2: forexsentimentdata.com
# --------------------------------------------------------------------------
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_NUM = r"(\d{1,3}(?:\.\d+)?)"


def parse_fsd(page: str, symbol: str) -> Pair:
    """Extract long/short % for `symbol` from the overview page.

    Strategy 1 reads the rendered table row ("XAUUSD 57.0 43.0 ...").
    Strategy 2 looks for the pair inside embedded JSON, in case the site
    renders its table with JavaScript.
    """
    text = _SCRIPT_RE.sub(" ", page)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", htmllib.unescape(text))
    for match in re.finditer(rf"\b{symbol}\b\s*{_NUM}\s+{_NUM}\b", text):
        long_pct, short_pct = float(match.group(1)), float(match.group(2))
        if valid_pair(long_pct, short_pct):
            return long_pct, short_pct

    json_pat = re.compile(
        rf'"{symbol}"[^{{}}]{{0,120}}?"long\w*"\s*:\s*"?{_NUM}"?[^{{}}]{{0,120}}?"short\w*"\s*:\s*"?{_NUM}',
        re.I)
    for match in json_pat.finditer(page):
        long_pct, short_pct = float(match.group(1)), float(match.group(2))
        if valid_pair(long_pct, short_pct):
            return long_pct, short_pct

    raise ValueError(f"no valid {symbol} row found on page")


def fetch_fsd(symbol: str) -> Pair:
    reply = requests.get(FSD_URL, headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                         timeout=HTTP_TIMEOUT)
    reply.raise_for_status()
    return parse_fsd(reply.text, symbol)


SOURCES: Dict[str, Fetcher] = {
    "mfx": fetch_myfxbook,
    "fsd": fetch_fsd,
}


# --------------------------------------------------------------------------
# State handling
# --------------------------------------------------------------------------
def load_previous(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def collect(prev: dict, now_ts: int, fetchers: Dict[str, Fetcher]) -> Tuple[dict, int]:
    """Query every source. A failing source never blocks the others; its last
    good value is carried forward (with its old timestamp) for MAX_CARRY_SEC."""
    out: dict = {"symbol": SYMBOL}
    fresh = 0
    for key, fetch in fetchers.items():
        try:
            long_pct, short_pct = fetch(SYMBOL)
        except Exception as exc:  # isolate each source; message is scrubbed below
            reason = scrub(f"{type(exc).__name__}: {exc}")
            log(f"::warning::{key} failed - {reason}")
            old_ts = prev.get(f"{key}_ts")
            if (isinstance(old_ts, (int, float)) and now_ts - old_ts <= MAX_CARRY_SEC
                    and f"{key}_long" in prev and f"{key}_short" in prev):
                out[f"{key}_long"] = prev[f"{key}_long"]
                out[f"{key}_short"] = prev[f"{key}_short"]
                out[f"{key}_ts"] = int(old_ts)
                log(f"{key}: keeping previous value ({now_ts - int(old_ts)}s old)")
            continue
        out[f"{key}_long"] = round(long_pct, 2)
        out[f"{key}_short"] = round(short_pct, 2)
        out[f"{key}_ts"] = now_ts
        fresh += 1
        log(f"{key}: long {long_pct:.1f}% / short {short_pct:.1f}%")
    return out, fresh


def _values_only(data: dict) -> dict:
    return {k: v for k, v in data.items() if k.endswith("_long") or k.endswith("_short")}


def atomic_write(path: str, data: dict) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".sent_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main() -> int:
    now_ts = int(time.time())
    prev = load_previous(OUT_FILE)
    data, fresh = collect(prev, now_ts, SOURCES)

    if fresh == 0:
        log("::error::all sources failed - leaving sentiment.json untouched")
        return 1

    data["updated_ts"] = now_ts
    data["updated"] = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    prev_age = now_ts - int(prev.get("updated_ts", 0) or 0)
    if _values_only(prev) == _values_only(data) and prev_age < HEARTBEAT_SEC:
        log("values unchanged - skipping write (avoids commit spam)")
        return 0

    atomic_write(OUT_FILE, data)
    log(f"wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
