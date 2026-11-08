#!/usr/bin/env python3
"""auth.py's own rules (ported from Fishbowl's tests/test_auth.py class Primitives at 47d22c4): TOTP vectors and window,
passwords, names, recovery codes, setup codes, the throttle. The HTTP side is tested in test_signin.py and the attack
runner, written for Subplot (Fishbowl's HTTP tests depend on its plain-HTTP and settings flows, which Subplot doesn't have)."""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "subplot"))
_SB = os.environ.get("SUBS_SANDBOX") or os.path.expanduser("~")
os.environ["SUBS_STATE"] = tempfile.mkdtemp(dir=_SB)
os.makedirs(os.path.join(os.environ["SUBS_STATE"], "auth"), mode=0o700)
import auth  # noqa: E402

PW = "plinth-marble-otter-cascade"


class _App:
    auth = auth


def load_app(**_):
    return _App


class Primitives(unittest.TestCase):
    def setUp(self):
        self.auth = load_app(AUTH="login").auth

    def test_rfc_vectors(self):
        a = self.auth
        hotp = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
        self.assertEqual([a.totp_code(b"12345678901234567890", i) for i in range(10)], hotp, "RFC 4226 appendix D")
        for t, code in [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"),
                        (2000000000, "69279037"), (20000000000, "65353130")]:
            self.assertEqual(a.totp_code(b"12345678901234567890", t // 30, 8), code, "RFC 6238 appendix B")

    def test_totp_window_and_replay(self):
        a, s, now = self.auth, self.auth.new_totp_secret(), 1_800_000_015
        step = now // 30
        for d in (-1, 0, 1):
            self.assertEqual(a.totp_match(s, a.totp_code(s, step + d), 0, now), step + d)
        for d in (-2, 2):
            self.assertIsNone(a.totp_match(s, a.totp_code(s, step + d), 0, now))
        self.assertIsNone(a.totp_match(s, a.totp_code(s, step), step, now), "a used code doesn't work again")
        self.assertIsNone(a.totp_match(s, a.totp_code(s, step - 1), step, now), "nor an older one")
        for junk in ("", "12345", "1234567", "abcdef", None, "12 34 5x"):
            self.assertIsNone(a.totp_match(s, junk, 0, now))
        self.assertEqual(len(a._b32(s)), 20, "160-bit secret")

    def test_hashing(self):
        a = self.auth
        h = a.hash_password(PW)
        self.assertRegex(h, r"^scrypt\$15\$8\$3\$")
        self.assertEqual(a.verify_password(PW, h), (True, False))
        self.assertEqual(a.verify_password(PW + "x", h), (False, False))
        self.assertEqual(a.verify_password("ＰＷ" + PW, a.hash_password("PW" + PW)), (True, False), "NFKC: the same password typed differently")
        old = a.hash_password.__wrapped__(PW) if hasattr(a.hash_password, "__wrapped__") else None
        self.assertIsNone(old)
        weaker = "scrypt$14$8$1$" + h.split("$", 5)[4] + "$" + h.split("$", 5)[5]
        self.assertEqual(a.verify_password(PW, weaker)[0], False, "params are part of the hash")
        for junk in ("", "scrypt$30$8$3$AAAA$BBBB", "md5$x", None, "scrypt$15$99$3$" + "A" * 24 + "$" + "B" * 44):
            self.assertEqual(a.verify_password(PW, junk), (False, False), junk)
        self.assertEqual(a.verify_password("x" * 5000, h), (False, False), "too long: refused, still costs a hash")

    def test_rehash_on_old_parameters(self):
        a = self.auth
        salt = b"s" * 16
        dk = __import__("hashlib").scrypt(PW.encode(), salt=salt, n=2 ** 14, r=8, p=1, maxmem=64 << 20, dklen=32)
        import base64
        old = f"scrypt$14$8$1${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
        self.assertEqual(a.verify_password(PW, old), (True, True))

    def test_password_rules(self):
        a = self.auth
        good = ["Tr0ub4dor&3-xylophone-q", "plinth marble otter cascade", "ünïcödé-pässwörd-łong", "x" * 14 + "y" + "zqw"]
        for pw in good:
            self.assertIsNone(a.password_problem(pw, "admin"), pw)
        bad = ["short", "fourteen-chars", "passwordpassword", "1q2w3e4r5t6y7u8i", "aaaaaaaaaaaaaaaaaaaa",
               "abcabcabcabcabcabc", "abcdefghijklmnopq", "0123456789012345678", "adminadminadmin1234", "subplot-subplot-12",
               "x" * 1025, 12345678901234567, None, "correct horse battery staple", "with\x00control-character"]
        for pw in bad:
            self.assertIsNotNone(a.password_problem(pw, "admin"), repr(pw)[:40])
        self.assertIsNotNone(a.name_problem(""))
        self.assertIsNotNone(a.name_problem("x" * 65))
        self.assertIsNotNone(a.name_problem("a\nb"))
        self.assertIsNone(a.name_problem("Sam"))

    def test_recovery_codes(self):
        codes, hashes = self.auth.new_recovery_codes()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        for c, h in zip(codes, hashes):
            self.assertRegex(c, r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){5}$")   # 24 characters from 32: 120 bits
            self.assertEqual(self.auth.rc_hash(c.lower().replace("-", " ")), h)
            self.assertNotIn(c, json.dumps(hashes))

    def test_sessions(self):
        a = self.auth
        s = a.Sessions()
        tok, max_age = s.create(True, "1.2.3.4", "UA", 1)
        self.assertEqual(max_age, 30 * 86400)
        self.assertGreaterEqual(len(tok), 43)
        with open(a.SESSIONS_FILE) as f:
            raw = f.read()
        self.assertNotIn(tok, raw, "only the token's hash is stored")
        self.assertEqual(oct(os.stat(a.SESSIONS_FILE).st_mode & 0o777), "0o600")
        self.assertIsNotNone(s.check(tok, 1))
        self.assertIsNone(s.check(tok, 2), "a session from another epoch (before a password change) doesn't count")
        tok = s.create(True, "1.2.3.4", "UA", 1)[0]
        for junk in ("", "x", tok + "x", tok[:-1], "../" * 20, None, "A" * 43):
            self.assertIsNone(s.check(junk, 1), junk)
        raw = open(a.SESSIONS_FILE).read()
        d = json.loads(raw)
        k = next(iter(d))
        d[k]["seen"] -= 8 * 86400   # idle longer than 7 days
        with open(a.SESSIONS_FILE, "w") as f:
            json.dump(d, f)
        self.assertIsNone(s.check(tok, 1), "idle too long")
        tok, _ = s.create(False, "ip", "ua", 1)
        d = json.loads(open(a.SESSIONS_FILE).read())
        d[a.Sessions._key(tok)]["until"] = int(time.time()) - 1
        open(a.SESSIONS_FILE, "w").write(json.dumps(d))
        self.assertIsNone(s.check(tok, 1), "past its absolute lifetime")
        t1, _ = s.create(False, "ip", "ua", 1)
        t2, _ = s.create(False, "ip", "ua", 1)
        s.end_all(but_token=t2)
        self.assertIsNone(s.check(t1, 1))
        self.assertIsNotNone(s.check(t2, 1))
        sid = s.listing(t2, 1)[0]["id"]
        self.assertTrue(s.end_id(sid))
        self.assertIsNone(s.check(t2, 1))
        open(a.SESSIONS_FILE, "w").write("{broken")
        self.assertEqual(s.listing(), [], "a damaged file signs everyone out, never in")
        for _ in range(60):
            s.create(False, "ip", "ua", 1)
        self.assertLessEqual(len(s.listing()), s.MAX)

    def test_throttle(self):
        t = self.auth.Throttle()
        for i in range(5):
            self.assertEqual(t.ip_wait("1.1.1.1", 1000.0), 0, i)
            t.ip_failed("1.1.1.1", 1000.0)
        self.assertGreater(t.ip_wait("1.1.1.1", 1000.0), 0)
        self.assertEqual(t.ip_wait("2.2.2.2", 1000.0), 0, "per address")
        self.assertEqual(t.wait_for(105, 0, 0), 901, "capped at 15 minutes")
        self.assertEqual(t.ip_wait("1.1.1.1", 1000.0 + 901), 0, "old failures age out of the 15-minute window")
        for _ in range(20000):
            t.ip_failed(str(_), 0)
        self.assertLessEqual(len(t.by_ip), 10001, "a flood of addresses can't grow it without bound")


class Epochs(unittest.TestCase):
    """A session counts only at the account's current epoch (SPEC-LOGIN security rule 3): a password or two-step change
    makes every older session worthless even if its record were still on disk."""

    def test_session_from_another_epoch_refused(self):
        s = auth.Sessions(os.path.join(os.environ["SUBS_STATE"], "auth", "epoch-sessions.json"))
        old, new = auth.new_epoch(), None
        new = auth.new_epoch(old)
        token, _ = s.create(False, "10.0.0.1", "test", old, True)
        self.assertTrue(s.check(token, old, "10.0.0.1", True), "valid at its own epoch")
        self.assertIsNone(s.check(token, new, "10.0.0.1", True), "refused at a newer epoch")
        self.assertIsNone(s.check(token, new, "10.0.0.1", True), "and again (the fast path)")


if __name__ == "__main__":
    unittest.main()
