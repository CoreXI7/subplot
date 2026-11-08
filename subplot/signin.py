"""Subplot's own sign-in (AUTH=login): the shared signin-core package (subplot/signin_core/, pinned by
subplot/signin_core.pin; contract in signin-core's SPEC.md) wired to the board. SPEC-LOGIN (Subplot v2).

What is Subplot's here (the hooks and names signin-core asks for):
- the board listens only on its unix socket, so every request comes through the reverse proxy (the socket is 0660,
  the proxy's group). There is no plain-HTTP sign-in: a request is HTTPS only when it carries exactly one
  X-Forwarded-Proto whose value is "https", and sessions are made and accepted only then.
- the client address (counting tries, the log, the account's events) is the single X-Real-IP when it parses as an IP
  address, otherwise "proxy" (one shared count). It never grants anything.
- errors use the board's key "why"; there is no next= redirect (the page goes to ./ after signing in).
With AUTH=proxy (the default) nothing here runs: server.py never calls into the mixin, and auth/ is never touched.
The vendored copy is checked against its pin before it is imported: a changed or half-synced copy stops the board.
"""
import ipaddress, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from signin_core import verify as _verify   # noqa: E402  (stdlib only; imports nothing else from the package)

_ok, _problems = _verify.check(HERE)
if not _ok:
    raise SystemExit("signin_core does not match signin_core.pin: " + "; ".join(_problems))

from signin_core import web, auth   # noqa: E402

AUTH = "login" if os.environ.get("SUBS_AUTH", "").strip() == "login" else "proxy"
CLI = "sudo runuser -u <service account> -- env SUBS_STATE=<state folder> python3 -I <code folder>/subplot/auth.py"
NOT_HTTPS = {"why": "Sign in over https: the reverse proxy must send X-Forwarded-Proto: https (and X-Real-IP)."}
PUBLIC_GET = frozenset({"/login", "/login.js", "/base.css", "/favicon.svg", "/api/auth/state"})
PUBLIC_POST = frozenset({"/api/login", "/api/login/code", "/api/setup-account", "/api/logout"})
ACCOUNT_POST = frozenset({"/api/account/password", "/api/account/totp/start", "/api/account/totp/confirm",
                          "/api/account/totp/off", "/api/account/recovery", "/api/account/sessions/end",
                          "/api/account/notice/dismiss"})
PAGES = frozenset({"/"})                 # without a session: 303 to login; any other path: 401

CONFIG = web.Config(
    auth=AUTH, app="Subplot", cookie="subplot", error_key="why", settings_label="Account",
    service_noun="the page's service", open_noun="the page", signin_first="sign in first",
    banned_words=("subplot", "subtitles", "plex"), cli_hint=CLI, reset_keeps="everything else stays",
    setup_code_hint="Use the newest one setup-code (or reset) printed: " + CLI + " setup-code",
    setup_code_where="the one setup-code or reset printed",
    run_as_hint="Run it as the service account (runuser -u <service account> --): files made by root would lock the page out.",
    data_dir=os.path.join(os.environ.get("SUBS_STATE", "/var/lib/subplot"), "auth"),
    public_get=PUBLIC_GET, public_post=PUBLIC_POST, pages=PAGES)
web.setup(CONFIG)                        # (no file is touched until used)


class SignIn(web.SignIn):
    """Mixed into server.Handler, which provides reply(code, payload, ctype, extra=None, cookies=()) and self.headers."""
    _token = None   # this request's session, looked up once (server.Handler resets it for every request)

    def send(self, code, body, extra=None, cookies=()):
        self.reply(code, body, extra=extra, cookies=cookies)

    def one_header(self, name):
        """A header the proxy sets, or None when it's missing or repeated (a proxy that appends instead of replacing
        would let the client pick the first copy)."""
        vals = self.headers.get_all(name) or []
        return vals[0].strip() if len(vals) == 1 else None

    def https(self):
        """Exactly one X-Forwarded-Proto, and it says https. Decides every cookie, session and sign-in."""
        return (self.one_header("X-Forwarded-Proto") or "").lower() == "https"

    def plain_http_ok(self):
        return self.https()              # no plain-HTTP sign-in on the board

    def client_ip(self):
        """The browser's address as the proxy tells it (one X-Real-IP that parses), else "proxy": one shared count."""
        try:
            a = ipaddress.ip_address(self.one_header("X-Real-IP") or "")
        except ValueError:
            return "proxy"
        if a.version == 6 and a.scope_id:
            return "proxy"
        return str(getattr(a, "ipv4_mapped", None) or a)

    def log(self, text):
        import common                    # (only when logging: the auth.py command line never needs the board's settings)
        common.log(text)

    def refuse_page(self, p, q):
        self.send(303, b"", extra={"Location": "login"})   # relative, so a path prefix in the proxy works

    def public_extra(self, p):
        return False

    def transport_refusal(self, p, post, stage):
        """The HTTPS hint instead of "sign in first" on the account paths, and every sign-in POST over HTTPS only."""
        if self.https():
            return None
        if stage == "post" or p in ACCOUNT_POST or p == "/api/account":
            return 403, NOT_HTTPS
        return None

    def auth_state_extra(self):
        return {}
