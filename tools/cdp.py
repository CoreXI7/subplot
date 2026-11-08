#!/usr/bin/env python3
"""Tiny Chrome DevTools Protocol client (stdlib only), used by tools/screenshots.py to capture one block of the page.
Starts headless Chrome on 127.0.0.1 with a throwaway profile, attaches to pages, evaluates JS, sends real
input events (so popups and fullscreen see a user gesture), sets files on <input type=file>, and collects
console errors and exceptions from every page."""
import base64, json, os, shutil, socket, struct, subprocess, tempfile, time, urllib.request

CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")


class WS:
    """Minimal client-side WebSocket (text frames, no extensions)."""

    def __init__(self, url):
        assert url.startswith("ws://")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.s.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.s.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("websocket handshake failed: " + head.decode(errors="replace"))
        self.buf = b""

    def send(self, text):
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        parts = []
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(n)
            op = b1 & 0x0F
            if op == 0x8:
                raise ConnectionError("websocket closed")
            if op in (0x1, 0x0):
                parts.append(payload)
                if b1 & 0x80:
                    return b"".join(parts).decode()


class Browser:
    def __init__(self, width=1600, height=900):
        self.dir = tempfile.mkdtemp(prefix="cdp-", dir=os.environ.get("SBX", os.path.expanduser("~/.cache/subplot-dev")))
        self.proc = None
        if os.environ.get("CDP_PORT"):      # attach to a browser started elsewhere (e.g. Edge on Windows via an SSH tunnel)
            self.port = int(os.environ["CDP_PORT"])
            self._connect()
            return
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={self.dir}/profile", "--no-first-run", "--no-default-browser-check",
             f"--window-size={width},{height}", "--disable-popup-blocking=false", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = os.path.join(self.dir, "profile", "DevToolsActivePort")
        for _ in range(200):
            if os.path.exists(port_file) and open(port_file).read().strip():
                break
            time.sleep(0.05)
        self.port = int(open(port_file).read().split()[0])
        self._connect()

    def _connect(self):
        ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version"))
        self.ws = WS(ver["webSocketDebuggerUrl"])
        self.next_id = 0
        self.events = []
        self.errors = []      # (session label, text)
        self.sessions = {}    # sessionId -> label
        self.call("Target.setDiscoverTargets", discover=True)

    def call(self, method, session=None, timeout=30, **params):
        self.next_id += 1
        mid = self.next_id
        msg = {"id": mid, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        end = time.time() + timeout
        while time.time() < end:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})
            self._event(m)
        raise TimeoutError(method)

    def _event(self, m):
        self.events.append(m)
        label = self.sessions.get(m.get("sessionId"), "?")
        if m.get("method") == "Runtime.exceptionThrown":
            d = m["params"]["exceptionDetails"]
            text = (d.get("exception") or {}).get("description") or d.get("text")
            self.errors.append((label, "exception: " + str(text)[:400]))
        elif m.get("method") == "Runtime.consoleAPICalled" and m["params"]["type"] in ("error", "assert"):
            args = " ".join(str(a.get("value", a.get("description", ""))) for a in m["params"]["args"])
            self.errors.append((label, "console.error: " + args[:400]))
        elif m.get("method") == "Log.entryAdded" and m["params"]["entry"]["level"] == "error":
            e = m["params"]["entry"]
            self.errors.append((label, f"log ({e.get('source')}): {e.get('text', '')[:400]}"))

    def pump(self, seconds):
        self.ws.s.settimeout(0.05)
        end = time.time() + seconds
        while time.time() < end:
            try:
                self._event(json.loads(self.ws.recv()))
            except (socket.timeout, TimeoutError):
                pass
        self.ws.s.settimeout(30)

    def attach(self, target_id, label):
        sid = self.call("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
        self.sessions[sid] = label
        for dom in ("Runtime.enable", "Log.enable", "Page.enable", "DOM.enable"):
            self.call(dom, session=sid)
        return sid

    def open(self, url, label):
        tid = self.call("Target.createTarget", url="about:blank")["targetId"]
        sid = self.attach(tid, label)
        self.call("Page.navigate", session=sid, url=url)
        self.pump(1.5)
        return sid

    def find_target(self, url_part):
        for t in self.call("Target.getTargets")["targetInfos"]:
            if t["type"] == "page" and url_part in t["url"]:
                return t["targetId"]
        return None

    def eval(self, sid, expr, await_promise=True):
        r = self.call("Runtime.evaluate", session=sid, expression=expr, returnByValue=True, awaitPromise=await_promise,
                      userGesture=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError("eval failed: " + str((d.get("exception") or {}).get("description") or d.get("text")))
        return r["result"].get("value")

    def click(self, sid, selector):
        box = self.eval(sid, f"(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
                             f" return [r.left + r.width / 2, r.top + r.height / 2]; }})()")
        for t in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", session=sid, type=t, x=box[0], y=box[1], button="left", clickCount=1)

    def key(self, sid, code, key=None, modifiers=0):
        key = key or code
        for t in ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", session=sid, type=t, code=code, key=key, modifiers=modifiers,
                      windowsVirtualKeyCode=VK.get(code, 0))

    def set_files(self, sid, selector, paths):
        doc = self.call("DOM.getDocument", session=sid)["root"]["nodeId"]
        node = self.call("DOM.querySelector", session=sid, nodeId=doc, selector=selector)["nodeId"]
        self.call("DOM.setFileInputFiles", session=sid, nodeId=node, files=paths)

    def close(self):
        if self.proc is None:
            shutil.rmtree(self.dir, ignore_errors=True)
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


VK = {"Space": 32, "ArrowUp": 38, "ArrowDown": 40, "PageUp": 33, "PageDown": 34, "Home": 36, "KeyB": 66, "KeyR": 82,
      "KeyV": 86, "KeyS": 83, "F2": 113, "Digit1": 49, "Digit2": 50, "Escape": 27}
