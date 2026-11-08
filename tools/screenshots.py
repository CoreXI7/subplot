#!/usr/bin/env python3
"""Render the Subplot README screenshots from a throw-away demo state.

Dev-only tool: it is not installed and not shipped. It writes a small demo
library of made-up shows, movies and subtitle lines, starts subplot/server.py
on a unix socket behind a 127.0.0.1-only forwarder, and drives headless Chrome
to save the PNGs under docs/img/.

Run it from the repository root:

    python3 tools/screenshots.py [--chrome PATH] [--out DIR]

Chrome or Chromium must be installed. Nothing here touches a real library, a
Plex server or a .env file; every title, path and line of dialogue is invented.
"""

import argparse
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "subplot"))
from common import BOARD_FIELDS  # noqa: E402  (constants only; the demo board gets its own env)

HOST = "subplot.example"
REPO = Path(__file__).resolve().parents[1]

_PROFILE = ""


def _iso(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(minutes):
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _srt_ts(ms):
    h, rem = divmod(int(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _srt(cues):
    out = []
    for i, (start, end, text) in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        out.append(text)
        out.append("")
    return "\n".join(out) + "\n"


SHOWS = [
    {
        "show": "Orbit Street",
        "show_plex_id": 1001,
        "year": 2024,
        "library": "TV Shows",
        "episodes": [
            (1, 1, "Pilot", "have"),
            (1, 2, "Streetlights", "have"),
            (1, 3, "The Corner Store", "have"),
            (1, 4, "Night Shift", "queued"),
            (1, 5, "Paper Route", "have"),
            (1, 6, "Late Bus", "have"),
            (2, 1, "New Neighbours", "have"),
            (2, 2, "The Fire Escape", "have"),
            (2, 3, "Sunday Market", "missing"),
            (2, 4, "The Long Way Round", "queued"),
            (2, 5, "Rooftop", "missing"),
            (2, 6, "Home", "missing"),
        ],
    },
    {
        "show": "The Lighthouse Keepers",
        "show_plex_id": 1002,
        "year": 2023,
        "library": "TV Shows",
        "episodes": [
            (1, 1, "First Light", "have"),
            (1, 2, "The Keeper's Log", "have"),
            (1, 3, "Fog", "burned_in"),
            (1, 4, "The Lamp Room", "have"),
            (1, 5, "Salvage", "review"),
            (1, 6, "Low Tide", "have"),
            (1, 7, "The Storm", "missing"),
            (1, 8, "Last Watch", "have"),
        ],
    },
    {
        "show": "Paper Moons",
        "show_plex_id": 1003,
        "year": 2022,
        "library": "TV Shows",
        "episodes": [
            (1, 1, "Origami", "have"),
            (1, 2, "The Fold", "have"),
            (1, 3, "Creases", "queued"),
            (1, 4, "Silver", "have"),
            (1, 5, "Rivers", "missing"),
            (1, 6, "Unfolding", "have"),
        ],
    },
    {
        "show": "Starfield Academy",
        "show_plex_id": 1004,
        "year": 2024,
        "library": "Anime",
        "episodes": [
            (1, 1, "Entrance Exam", "have"),
            (1, 2, "The Dorm", "have"),
            (1, 3, "Zero-G Gym", "queued"),
            (1, 4, "Comet Club", "have"),
            (1, 5, "Night Study", "missing"),
            (1, 6, "The Observatory", "have"),
            (1, 7, "Meteor Shower", "missing"),
            (1, 8, "Field Trip to Mars", "review"),
            (1, 9, "The Rival", "have"),
            (1, 10, "Exam Week", "queued"),
            (1, 11, "Orbit Festival", "burned_in"),
            (1, 12, "Graduation", "have"),
        ],
    },
]

MOVIES = [
    ("Big Buck Bunny", 2008, "have"),
    ("Sintel", 2010, "have"),
    ("Tears of Steel", 2012, "missing"),
    ("Spring", 2019, "queued"),
    ("Cosmos Laundromat", 2015, "have"),
]

WHISPER_PREVIEW = [
    [12400, 15800, "The lamp needs oil before the fog comes in."],
    [15900, 19200, "You said that last night, and the fog came anyway."],
    [19600, 23100, "Then I'll say it again. Hand me the can."],
    [23400, 26800, "Careful on the stairs, they're slick."],
    [27100, 30900, "I've climbed them in worse weather than this."],
    [31200, 35500, "Not with your knee, you haven't."],
]

OPENSUBTITLES_PREVIEW = [
    [8100, 11800, "Zero-G drills start in ten minutes."],
    [12000, 15300, "I still can't tie my boots in this thing."],
    [15600, 18900, "Nobody can. That's why it's a drill."],
    [19200, 22400, "Remind me why we signed up for the comet club?"],
    [22700, 26200, "Because the food is better up here."],
    [26500, 30100, "It's the same food, Kaede."],
]

REVIEWS = {
    "ep-117": {
        "source": "whisper large-v3",
        "confidence": 0.72,
        "why": "confidence 0.72 is below keep 0.85",
        "offset_ms": None,
        "shifted_by": None,
        "preview": WHISPER_PREVIEW,
    },
    "ep-134": {
        "source": "opensubtitles",
        "confidence": 0.94,
        "why": "timing not confirmed",
        "offset_ms": 2400,
        "shifted_by": None,
        "preview": OPENSUBTITLES_PREVIEW,
    },
}

DONE = [
    (5, "ep-108", "place", "placed", "placed en", "opensubtitles", 0.95),
    (12, "ep-134", "find", "review", "timing not confirmed", "opensubtitles", 0.94),
    (19, "ep-110", "gen", "queued", "waiting for the nightly slot", "whisper", None),
    (26, "ep-117", "gen", "review", "confidence below keep 0.85", "whisper", 0.72),
    (33, "ep-113", "find", "found", "best match 0.96", "opensubtitles", 0.96),
    (40, "ep-114", "sync", "placed", "synced, offset 180 ms", "opensubtitles", 0.97),
    (47, "ep-115", "gen", "rejected", "burned-in text not readable", "whisper", 0.41),
    (54, "ep-116", "place", "placed", "placed en", "opensubtitles", 0.92),
    (61, "ep-118", "find", "found", "best match 0.89", "opensubtitles", 0.89),
    (68, "ep-112", "find", "failed", "no English subtitle found", "opensubtitles", None),
    (75, "ep-120", "place", "placed", "placed en", "opensubtitles", 0.93),
    (82, "ep-121", "find", "found", "best match 0.95", "opensubtitles", 0.95),
    (89, "ep-122", "place", "placed", "placed en", "opensubtitles", 0.94),
    (96, "ep-124", "sync", "placed", "shifted 240 ms", "opensubtitles", 0.96),
    (103, "ep-125", "find", "failed", "episode not matched", "opensubtitles", None),
    (110, "ep-127", "find", "found", "best match 0.98", "opensubtitles", 0.98),
    (117, "ep-128", "place", "placed", "placed en", "opensubtitles", 0.97),
    (124, "ep-129", "gen", "queued", "queued for transcription", "whisper", None),
    (131, "ep-131", "find", "failed", "no release matching the episode", "opensubtitles", None),
    (138, "ep-130", "place", "placed", "placed en", "opensubtitles", 0.93),
    (146, "ep-133", "sync", "placed", "shifted 320 ms", "opensubtitles", 0.95),
    (153, "ep-135", "place", "placed", "placed en", "opensubtitles", 0.96),
    (160, "ep-138", "place", "placed", "placed en", "opensubtitles", 0.94),
    (168, "mv-201", "find", "found", "best match 0.99", "opensubtitles", 0.99),
    (174, "mv-205", "place", "placed", "placed en", "opensubtitles", 0.97),
]


def build_demo(root):
    """Write the demo state, rules.json and media folders under root."""
    root = Path(root)
    state = root / "state"
    media = root / "media"
    for sub in ("tv", "movies", "anime"):
        (media / sub).mkdir(parents=True, exist_ok=True)
    (root / "sock").mkdir(parents=True, exist_ok=True)
    for sub in ("review", "marks", "queue", "settings"):
        (state / sub).mkdir(parents=True, exist_ok=True)

    items = {}
    episode_seq = 101
    plex_seq = 9101
    for show in SHOWS:
        for season, episode, title, status in show["episodes"]:
            key = f"ep-{episode_seq}"
            items[key] = {
                "key": key,
                "plex_id": plex_seq,
                "kind": "episode",
                "library": show["library"],
                "show_plex_id": show["show_plex_id"],
                "show": show["show"],
                "title": title,
                "season": season,
                "episode": episode,
                "year": show["year"],
                "need": [] if status == "have" else ["en"],
                "status": status,
                "path": f"/data/tv/{show['show']}/S{season:02d}E{episode:02d}.mkv",
            }
            episode_seq += 1
            plex_seq += 1

    movie_seq = 201
    movie_plex = 9201
    for title, year, status in MOVIES:
        key = f"mv-{movie_seq}"
        items[key] = {
            "key": key,
            "plex_id": movie_plex,
            "kind": "movie",
            "library": "Movies",
            "title": title,
            "year": year,
            "need": [] if status == "have" else ["en"],
            "status": status,
            "path": f"/data/movies/{title} ({year})/{title}.mkv",
        }
        movie_seq += 1
        movie_plex += 1

    _write_json(state / "library.json", {"items": items})
    # the board reads only this trimmed copy (common.write_board_index; scan.py writes it on a real install)
    _write_json(state / "board-index.json", {"at": None, "items": {k: {f: v[f] for f in BOARD_FIELDS if f in v}
                                                                   for k, v in items.items()}})

    _write_json(state / "coverage.json", {
        "at": "2026-11-08T05:16:00Z",
        "stale": False,
        "libraries": {
            "TV Shows": {"items": 1240, "en": 1105, "es": 210, "none": 120,
                         "image_only": 15, "generatable": 120},
            "Movies": {"items": 480, "en": 455, "es": 40, "none": 20,
                       "image_only": 5, "generatable": 20},
            "Anime": {"items": 320, "en": 300, "es": 15, "none": 12,
                      "image_only": 3, "generatable": 12},
        },
    })

    for key, body in REVIEWS.items():
        _write_json(state / "review" / f"{key}.en.json", body)
        _write_text(state / "review" / f"{key}.en.srt", _srt(body["preview"]))

    placed = 0
    for key, item in items.items():
        if item["status"] == "have":
            _write_json(state / "marks" / f"{key}.json",
                        {"at": _ago(200 + placed * 6), "placed": True})
            placed += 1
        elif item["status"] == "burned_in":
            _write_json(state / "marks" / f"{key}.json",
                        {"at": _ago(90), "burned_in": True})

    queue_srt = _srt([
        (1500, 4200, "We should go back before the lights come on."),
        (4300, 7600, "Not yet. One more street."),
        (7700, 11200, "You always say one more."),
    ])
    for key, item in items.items():
        if item["status"] == "queued":
            _write_text(state / "queue" / f"{key}.en.srt", queue_srt)

    done_lines = []
    for minutes, key, step, result, why, source, confidence in DONE:
        item = items[key]
        done_lines.append(json.dumps({
            "at": _ago(minutes),
            "step": step,
            "key": key,
            "show": item.get("show"),
            "title": item["title"],
            "season": item.get("season"),
            "episode": item.get("episode"),
            "library": item["library"],
            "lang": "en",
            "result": result,
            "why": why,
            "source": source,
            "confidence": confidence,
        }, ensure_ascii=False))
    _write_text(state / "done.log", "\n".join(done_lines) + "\n")

    _write_json(state / "budget.json", {
        "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "downloads": 312,
    })

    _write_json(state / "pc.json", {
        "at": _iso(),
        "idle": True,
        "why": "idle",
        "idle_since": _ago(42),
        "last_run": _ago(38),
        "gpu_util": 3,
    })

    _write_json(state / "current.json", {
        "at": _iso(),
        "step": "gen",
        "key": "ep-110",
        "show": "Orbit Street",
        "title": "The Long Way Round",
        "season": 2,
        "episode": 4,
        "library": "TV Shows",
        "lang": "en",
        "phase": "transcribing",
        "n": 3,
        "of": 12,
        "done_ms": 812000,
        "of_ms": 1420000,
        "started_at": _ago(9),
    })

    _write_json(state / "settings" / "settings.json", {
        "version": 3,
        "at": _iso(),
        "by": "page",
        "values": {
            "find": {"daily_budget": 600},
            "thresholds": {"keep": 0.9},
            "exclude_libraries": ["Kids Corner"],
        },
    })
    history = [
        {"at": _ago(190), "by": "page", "version": 1, "changed": ["find.daily_budget"]},
        {"at": _ago(120), "by": "page", "version": 2, "changed": ["thresholds.keep"]},
        {"at": _ago(25), "by": "page", "version": 3, "changed": ["exclude_libraries"]},
    ]
    _write_text(state / "settings" / "history.log",
                "\n".join(json.dumps(line) for line in history) + "\n")

    _write_json(root / "rules.json", {
        "path_map": {
            "/data/tv": str(media / "tv"),
            "/data/movies": str(media / "movies"),
        },
        "media_roots": [str(media)],
        "plex": {"machine_id": "0123456789abcdef0123456789abcdef"},
        "pc_idle": {
            "hours": ["mon-fri 00:00-17:00", "sat-sun 02:00-09:00"],
            "unload_idle_model": {
                "ollama": "http://10.0.0.50:11434",
                "after_min": 10,
            },
        },
    })

    return {
        "root": str(root),
        "state": str(state),
        "rules": str(root / "rules.json"),
        "sock": str(root / "sock" / "board.sock"),
        "media": str(media),
    }


def start_board(repo, root):
    """Start subplot/server.py with the demo env and return the Popen."""
    root = Path(root)
    env = dict(os.environ)
    env.update({
        "SUBS_STATE": str(root / "state"),
        "SUBS_RULES": str(root / "rules.json"),
        "SUBS_HOST": HOST,
        "SUBS_SOCK": str(root / "sock" / "board.sock"),
        "SUBS_AUTH": "proxy",
        "SUBS_ENV": str(root / "none.env"),
        "SUBS_NAV_FILE": "",
        "SUBS_OVERLAY": "",
    })
    cmd = [sys.executable, "-I", str(Path(repo) / "subplot" / "server.py")]
    return subprocess.Popen(cmd, cwd=str(repo), env=env)


def wait_socket(proc, sock_path, timeout=10.0):
    sock_path = Path(sock_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock_path.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError(f"board exited with code {proc.returncode}")
        time.sleep(0.1)
    raise RuntimeError("board socket did not appear within 10 s")


def forwarder(sock_path):
    """TCP 127.0.0.1 forwarder that fixes Host and speaks to the board socket."""
    sock_path = Path(sock_path)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                    if len(buf) > 1 << 20:
                        return
                head, _, body = buf.partition(b"\r\n\r\n")
                lines = [ln for ln in head.split(b"\r\n") if ln]
                rewritten = []
                for ln in lines:
                    name = ln.split(b":", 1)[0].strip().lower()
                    if name in (b"host", b"connection", b"proxy-connection"):
                        continue
                    if name == b"origin":                     # the page's real origin behind a proxy
                        ln = b"Origin: https://" + HOST.encode("ascii")
                    if name == b"content-length":
                        want = int(ln.split(b":", 1)[1].strip() or 0)
                        while len(body) < want:
                            chunk = self.request.recv(65536)
                            if not chunk:
                                break
                            body += chunk
                    rewritten.append(ln)
                rewritten.append(b"Host: " + HOST.encode("ascii"))
                rewritten.append(b"Connection: close")
                payload = b"\r\n".join(rewritten) + b"\r\n\r\n" + body

                upstream = socket.socket(getattr(socket, "AF_UNIX"), socket.SOCK_STREAM)
                try:
                    upstream.connect(str(sock_path))
                    upstream.sendall(payload)
                    while True:
                        chunk = upstream.recv(65536)
                        if not chunk:
                            break
                        self.request.sendall(chunk)
                finally:
                    upstream.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, name="subplot-forwarder", daemon=True
    )
    thread.start()
    return server, port


def wait_state(port, timeout=10.0):
    """Wait until GET /api/state answers 200 through the forwarder."""
    url = f"http://127.0.0.1:{port}/api/state"
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
                last = f"HTTP {resp.status}"
        except Exception as exc:
            last = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"/api/state did not answer 200 through the forwarder: {last}")


def shoot(chrome, url, out, w, h, scale=1):
    profile = Path(_PROFILE or "chrome-profile")
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--user-data-dir=" + str(profile),
        f"--window-size={w},{h}",
        f"--force-device-scale-factor={scale}",
        "--virtual-time-budget=4000",
        "--force-prefers-reduced-motion",
        "--screenshot=" + str(out),
        url,
    ]
    subprocess.run(cmd, timeout=60, check=True)


def shoot_block(chrome, url, out, element_id, w, max_h):
    """Capture one block of the page (the one-shot --screenshot mode can't scroll): load the page over DevTools, wait for
    the block to fill, and clip the screenshot to it (at most max_h tall)."""
    import base64, time
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ["CHROME"] = chrome
    from cdp import Browser
    b = Browser(width=w, height=max_h)
    try:
        sid = b.open(url, "page")
        b.call("Emulation.setEmulatedMedia", session=sid, features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        end = time.time() + 15
        while time.time() < end:
            b.pump(0.5)
            if b.eval(sid, f"!!document.querySelector('#{element_id} fieldset')"):
                break
        b.pump(1.0)
        box = b.eval(sid, f"(() => {{ const e = document.getElementById('{element_id}'); e.open = true; e.scrollIntoView();"
                          f" const r = e.getBoundingClientRect(); return [r.left, r.top + window.scrollY, r.width, r.height]; }})()")
        b.pump(0.5)
        clip = {"x": 0, "y": max(0, box[1] - 16), "width": w, "height": min(max_h, box[3] + 32), "scale": 1}
        data = b.call("Page.captureScreenshot", session=sid, format="png", clip=clip, captureBeyondViewport=True)["data"]
        Path(out).write_bytes(base64.b64decode(data))
    finally:
        b.close()


def main(argv=None):
    global _PROFILE

    parser = argparse.ArgumentParser(
        description="Render docs/img screenshots from a throw-away demo state."
    )
    parser.add_argument(
        "--chrome",
        default=os.environ.get("CHROME", "/usr/bin/google-chrome"),
        help="Chrome/Chromium binary (default: $CHROME or /usr/bin/google-chrome)",
    )
    parser.add_argument(
        "--out",
        default=str(REPO / "docs" / "img"),
        help="output folder (default: <repo>/docs/img)",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    sbx = Path(os.environ.get("SBX", os.path.expanduser("~/.cache/subplot-dev")))
    sbx.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(sbx), prefix="subplot-shots-"))
    _PROFILE = str(root / "chrome-profile")

    board = None
    server = None
    try:
        demo = build_demo(root)
        sock = Path(demo["sock"])

        board = start_board(REPO, root)
        wait_socket(board, sock, 10.0)

        server, port = forwarder(sock)
        base = f"http://127.0.0.1:{port}"

        shots = [
            ("overview.png", base + "/", 1280, 1180, 1),
            ("phone.png", base + "/", 390, 844, 2),
            ("settings.png", base + "/#settings", 1280, 1100, 1),
        ]
        for name, url, w, h, scale in shots[:2]:
            wait_state(port, 10.0)
            shoot(args.chrome, url, out_dir / name, w, h, scale)
        wait_state(port, 10.0)
        shoot_block(args.chrome, base + "/#settings", out_dir / "settings.png", "settings-box", 1280, 1100)

        for name, _url, _w, _h, _scale in shots:
            path = out_dir / name
            if not path.exists():
                print(f"{name}: MISSING")
                continue
            size = path.stat().st_size
            print(f"{name}: {size / 1024:.0f} KB")
            if size > 400 * 1024:
                print(f"warning: {name} is over 400 KB")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if board is not None and board.poll() is None:
            board.terminate()
            try:
                board.wait(timeout=5)
            except subprocess.TimeoutExpired:
                board.kill()
                board.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
