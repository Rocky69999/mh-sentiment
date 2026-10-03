"""Offline tests for sentiment.py (no network). Run: python -m unittest -v"""
import json
import os
import tempfile
import unittest
from unittest import mock

import sentiment as S

# Rendered text of the real overview table, as seen on 2026-10-02 (XAUUSD 57.0 / 43.0)
FSD_TABLE_HTML = """
<html><head><style>.x{color:red}</style><script>var XAUUSD = 1 2;</script></head><body>
<table>
<tr><td><a href="/chart?pair=EURUSD">EURUSD</a></td><td>87.0</td><td>13.0</td><td>-1.0</td></tr>
<tr><td><a href="/chart?pair=XAUUSD">XAUUSD</a></td><td>57.0</td><td>43.0</td><td>-3.0</td><td>&mdash;</td></tr>
<tr><td><a href="/chart?pair=AUDCAD">AUDCAD</a></td><td>44.0</td><td>56.0</td></tr>
</table></body></html>
"""
FSD_JSON_HTML = '<html><script id="d">{"pairs":[{"pair":"EURUSD","long":87,"short":13},' \
                '{"pair":"XAUUSD","longPct":"61.5","shortPct":"38.5"}]}</script></html>'

MFX_LOGIN_OK = {"error": False, "message": "", "session": "SESSION123"}
MFX_OUTLOOK = {"error": False, "message": "", "symbols": [
    {"name": "EURUSD", "shortPercentage": 55, "longPercentage": 44},
    {"name": "XAUUSD", "shortPercentage": 38, "longPercentage": 61}]}


class FakeResp:
    def __init__(self, payload=None, text="", status=200):
        self._p, self.text, self.status_code = payload, text, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise S.requests.HTTPError(f"{self.status_code} for url: https://x/?password=SECRETPW")


class ParseFsd(unittest.TestCase):
    def test_table_row(self):
        self.assertEqual(S.parse_fsd(FSD_TABLE_HTML, "XAUUSD"), (57.0, 43.0))

    def test_embedded_json(self):
        self.assertEqual(S.parse_fsd(FSD_JSON_HTML, "XAUUSD"), (61.5, 38.5))

    def test_missing_pair(self):
        with self.assertRaises(ValueError):
            S.parse_fsd("<td>EURUSD</td><td>50</td><td>50</td>", "XAUUSD")

    def test_rejects_values_that_do_not_add_up(self):
        with self.assertRaises(ValueError):
            S.parse_fsd("<td>XAUUSD</td><td>57.0</td><td>12.0</td>", "XAUUSD")


class Myfxbook(unittest.TestCase):
    def setUp(self):
        os.environ["MFX_EMAIL"], os.environ["MFX_PASSWORD"] = "me@x.com", "SECRETPW"

    def test_happy_path_and_logout(self):
        calls = []
        test = self

        def fake_get(_session, url, params=None, timeout=None):
            calls.append(url.rsplit("/", 1)[-1])
            if url.endswith("login.json"):
                return FakeResp(MFX_LOGIN_OK)
            if url.endswith("get-community-outlook.json"):
                test.assertEqual(params["session"], "SESSION123")
                return FakeResp(MFX_OUTLOOK)
            return FakeResp({"error": False})

        with mock.patch.object(S.requests.Session, "get", fake_get):
            self.assertEqual(S.fetch_myfxbook("XAUUSD"), (61.0, 38.0))
        self.assertEqual(calls, ["login.json", "get-community-outlook.json", "logout.json"])

    def test_login_rejected(self):
        with mock.patch.object(S.requests.Session, "get",
                               lambda *a, **k: FakeResp({"error": True, "message": "Invalid email or password"})):
            with self.assertRaises(RuntimeError):
                S.fetch_myfxbook("XAUUSD")

    def test_missing_credentials(self):
        os.environ["MFX_EMAIL"] = ""
        with self.assertRaises(RuntimeError):
            S.fetch_myfxbook("XAUUSD")

    def test_error_text_never_contains_password(self):
        with mock.patch.object(S.requests.Session, "get", lambda *a, **k: FakeResp(status=500)):
            try:
                S.fetch_myfxbook("XAUUSD")
            except Exception as exc:  # same path as collect()
                self.assertNotIn("SECRETPW", S.scrub(f"{type(exc).__name__}: {exc}"))


class Collect(unittest.TestCase):
    NOW = 1_790_000_000

    def test_both_ok(self):
        out, fresh = S.collect({}, self.NOW, {"mfx": lambda s: (61, 38), "fsd": lambda s: (57, 43)})
        self.assertEqual(fresh, 2)
        self.assertEqual(out["mfx_long"], 61)
        self.assertEqual(out["fsd_ts"], self.NOW)

    def test_failed_source_keeps_recent_value_with_old_ts(self):
        prev = {"fsd_long": 55, "fsd_short": 45, "fsd_ts": self.NOW - 600}

        def boom(_):
            raise ValueError("blocked")
        out, fresh = S.collect(prev, self.NOW, {"mfx": lambda s: (61, 38), "fsd": boom})
        self.assertEqual(fresh, 1)
        self.assertEqual((out["fsd_long"], out["fsd_ts"]), (55, self.NOW - 600))

    def test_failed_source_drops_value_after_carry_limit(self):
        prev = {"fsd_long": 55, "fsd_short": 45, "fsd_ts": self.NOW - S.MAX_CARRY_SEC - 1}

        def boom(_):
            raise ValueError("blocked")
        out, _ = S.collect(prev, self.NOW, {"mfx": lambda s: (61, 38), "fsd": boom})
        self.assertNotIn("fsd_long", out)


class MainFlow(unittest.TestCase):
    def run_main(self, path, fetchers, now):
        with mock.patch.object(S, "OUT_FILE", path), mock.patch.object(S, "SOURCES", fetchers), \
                mock.patch.object(S.time, "time", return_value=now):
            return S.main()

    def test_write_skip_heartbeat_and_total_failure(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sentiment.json")
            ok = {"mfx": lambda s: (61, 38), "fsd": lambda s: (57, 43)}
            t0 = 1_790_000_000

            self.assertEqual(self.run_main(path, ok, t0), 0)
            first = json.load(open(path))
            self.assertEqual(first["updated_ts"], t0)

            # same values 20 min later -> file untouched
            self.assertEqual(self.run_main(path, ok, t0 + 1200), 0)
            self.assertEqual(json.load(open(path))["updated_ts"], t0)

            # heartbeat after 55+ min -> rewritten
            self.assertEqual(self.run_main(path, ok, t0 + S.HEARTBEAT_SEC + 5), 0)
            self.assertEqual(json.load(open(path))["updated_ts"], t0 + S.HEARTBEAT_SEC + 5)

            # changed value -> rewritten immediately
            changed = {"mfx": lambda s: (62, 37), "fsd": lambda s: (57, 43)}
            self.assertEqual(self.run_main(path, changed, t0 + S.HEARTBEAT_SEC + 100), 0)
            self.assertEqual(json.load(open(path))["mfx_long"], 62)

            # everything fails -> exit 1 and file kept
            def boom(_):
                raise ValueError("down")
            before = open(path).read()
            self.assertEqual(self.run_main(path, {"mfx": boom, "fsd": boom}, t0 + 9999), 1)
            self.assertEqual(open(path).read(), before)


if __name__ == "__main__":
    unittest.main()
