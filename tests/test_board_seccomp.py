"""The board must start and answer under the units' own syscall filter (a chown in make_server once killed it with SIGSYS
on every start). Runs the real server.py in a transient user unit with the same SystemCallFilter lines as the template;
skipped where systemd-run --user is not available."""
import os, re, shutil, socket, subprocess, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def filter_lines():
    text = open(os.path.join(ROOT, "units", "board.service.in")).read()
    return [l for l in text.split("\n") if l.startswith(("SystemCallFilter=", "SystemCallArchitectures="))]


def user_systemd():
    if not shutil.which("systemd-run"):
        return False
    return subprocess.run(["systemd-run", "--user", "--quiet", "--wait", "--collect", "true"], capture_output=True, timeout=60).returncode == 0


@unittest.skipUnless(user_systemd(), "no systemd --user here")
class BoardUnderFilter(unittest.TestCase):
    def test_board_starts_and_answers(self):
        base = tempfile.mkdtemp(prefix="subplot-seccomp-", dir=os.environ.get("SUBS_SANDBOX") or os.path.expanduser("~"))
        state, cfg, run = (os.path.join(base, d) for d in ("state", "cfg", "run"))
        for d in (state, cfg, run):
            os.makedirs(d)
        with open(os.path.join(cfg, "rules.json"), "w") as f:
            f.write("{}")
        sock = os.path.join(run, "b.sock")
        unit = f"subplot-seccomp-{os.getpid()}"
        props = []
        for line in filter_lines():
            props += ["-p", line]
        env = [f"--setenv={k}={v}" for k, v in (("SUBS_STATE", state), ("SUBS_RULES", os.path.join(cfg, "rules.json")),
                                                  ("SUBS_ENV", os.path.join(cfg, ".env")), ("SUBS_HOST", "subs.example.com"),
                                                  ("SUBS_SOCK", sock), ("SUBS_SOCK_GROUP", ""), ("SUBS_MARKER_TAG", "subplot"))]
        subprocess.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}", *props, *env,
                        sys.executable, "-I", os.path.join(ROOT, "subplot", "server.py")], check=True, timeout=60)
        try:
            for _ in range(50):
                if os.path.exists(sock):
                    break
                time.sleep(0.2)
            self.assertTrue(os.path.exists(sock), "the board never created its socket (killed by the filter?)")
            time.sleep(0.5)
            c = socket.socket(socket.AF_UNIX); c.settimeout(5); c.connect(sock)
            c.sendall(b"GET /api/config HTTP/1.1\r\nHost: subs.example.com\r\nConnection: close\r\n\r\n")
            reply = b""
            while (b := c.recv(65536)):
                reply += b
            c.close()
            self.assertTrue(reply.startswith(b"HTTP/1.1 200"), reply[:80])
            self.assertEqual(os.stat(sock).st_mode & 0o777, 0o660)
        finally:
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=60)
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
