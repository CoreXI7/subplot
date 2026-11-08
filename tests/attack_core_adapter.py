"""Subplot's adapter for signin-core's attack runner (signin-core tests/attack_core.py): a real board in login mode on a
unix socket in the sandbox; HTTPS and the client address arrive as the reverse proxy sends them."""
import os, socket, subprocess, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = "subs.example"


class Adapter:
    name, cookie = "subplot", "subplot"
    pages = ("/",)
    public_get = ("/login", "/login.js", "/base.css", "/favicon.svg", "/api/auth/state")
    private_get = ("/api/state", "/api/missing?q=a", "/api/show/1")

    def start(self, base):
        self.base, self.sock, self.lines = base, os.path.join(base, "b.sock"), []
        os.makedirs(os.path.join(base, "state", "auth"), 0o700)
        with open(os.path.join(base, "rules.json"), "w") as f:
            f.write("{}")
        self.env = {**os.environ, "SUBS_STATE": os.path.join(base, "state"), "SUBS_SOCK": self.sock, "SUBS_HOST": HOST,
                    "SUBS_AUTH": "login", "SUBS_RULES": os.path.join(base, "rules.json"), "SUBS_SOCK_GROUP": "",
                    "SUBS_HOME": base}
        self.proc = subprocess.Popen([sys.executable, "-I", "subplot/server.py"], env=self.env, cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=lambda: [self.lines.append(l) for l in self.proc.stdout], daemon=True).start()
        for _ in range(100):
            if os.path.exists(self.sock):
                return
            time.sleep(0.1)
        raise RuntimeError("the board did not start: " + "".join(self.lines)[-400:])

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def log(self):
        return "".join(self.lines)

    def setup_code(self):
        out = subprocess.run([sys.executable, "-I", "subplot/auth.py", "setup-code"], env=self.env, cwd=ROOT,
                             capture_output=True, text=True).stdout
        return out.split()[-1]

    def send(self, raw):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        try:
            s.connect(self.sock)
            s.sendall(raw)
            out = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return out
                out += chunk
        except socket.timeout:
            return b"TIMEOUT"
        except OSError as exc:
            return repr(exc).encode()
        finally:
            s.close()

    def headers(self, ip):
        return [("Host", HOST), ("X-Forwarded-Proto", "https"), ("X-Real-IP", ip)]

    def csrf_ok(self):
        return [("X-Subs", "1")]

    def csrf_bad(self):
        return [("no-X-Subs", []), ("foreign-Origin", [("X-Subs", "1"), ("Origin", "https://evil.example")]),
                ("cross-site", [("X-Subs", "1"), ("Sec-Fetch-Site", "cross-site")])]
