#!/usr/bin/env python3
"""Subplot installer (run through ../install.sh with sudo). Reads CONFIG_DIR/subplot.conf, checks the host, renders the
unit templates in units/ and installs them into /etc/systemd/system.

  sudo ./install.sh [/etc/subplot/subplot.conf]          check, render, install, enable
  ./install.sh --check [conf]                            check what can be checked and render into a temp dir; no changes

Every value must match its pattern below; nothing outside that pattern can reach a unit file, so no quoting, escaping or
systemd specifier can be smuggled in. A line of a template whose token is empty (an optional key left blank) is left out;
a line holding @MEDIA_ROOT@ is repeated once per media root. Nothing is ever written outside /etc/systemd/system, the
state folder and the socket folder."""
import errno, grp, hashlib, json, os, pwd, re, shutil, stat, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
UNITS = os.path.join(REPO, "units")
SYSTEMD = "/etc/systemd/system"
DEFAULT_CONF = "/etc/subplot/subplot.conf"
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"

PATH = r"/[A-Za-z0-9_./-]{1,200}"
NAME = r"[a-z_][a-z0-9_-]{0,31}"
HOSTNAME = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
PATTERNS = {                                       # key: (regex, required)
    "RUN_USER": (NAME, True),
    "CODE_DIR": (PATH, True),
    "STATE_DIR": (PATH, True),
    "CONFIG_DIR": (PATH, True),
    "VENV": (PATH, True),
    "MEDIA_ROOTS": (PATH + r"(?: " + PATH + r"){0,15}", True),
    "PLEX_DB": (r"/[A-Za-z0-9_./ -]{1,250}", True),            # the one key that may hold spaces: it is always quoted
    "PC_HOST": (r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", True),
    "PC_IP": (r"(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}", True),
    "PC_ROOT": (r"[A-Za-z]:/[A-Za-z0-9_-]{1,60}", True),
    "SOCKET": (PATH, True),
    "PROXY_GROUP": (NAME, True),
    "PUBLIC_HOST": (HOSTNAME, True),
    "MARKER_TAG": (r"[a-z][a-z0-9-]{2,31}", False),
    "USER_AGENT": (r"[A-Za-z0-9._/-]{1,20} v[0-9.]{1,10}", False),
    "UNIT_PREFIX": (r"subplot|subs", False),
    "OVERLAY_DIR": (PATH, False),
    "HIDE_PATHS": (PATH + r"(?: " + PATH + r"){0,15}", False),   # extra mounts the services without media access must not see
    "AUTH": (r"proxy|login", False),                               # login: the board's own sign-in (SPEC-LOGIN)
}
DEFAULTS = {"MARKER_TAG": "subplot", "USER_AGENT": "subplot v1.0", "UNIT_PREFIX": "subplot", "OVERLAY_DIR": "", "HIDE_PATHS": "", "AUTH": "proxy"}
REPEAT = {"MEDIA_ROOT": "MEDIA_ROOTS", "HIDE_PATH": "HIDE_PATHS"}          # a line with @MEDIA_ROOT@ repeats per media root
PATH_KEYS = ("CODE_DIR", "STATE_DIR", "CONFIG_DIR", "VENV", "SOCKET", "OVERLAY_DIR", "PLEX_DB")
STEPS = ("scan", "find", "sync", "place", "gen", "board")
TIMERS = ("scan", "find", "sync", "place", "gen")
STATE_SUBDIRS = ("incoming", "queue", "review", "decisions", "marks", "requests", "pc", "tokens", "settings")


class Refused(Exception):
    """A value or a check failed; the message says what to fix."""


def _path_ok(value):
    """Absolute, already normal (no '.', '..' or '//' segments) and not '/' itself."""
    return value != "/" and os.path.normpath(value) == value and "//" not in value


def parse_conf(text):
    """{key: value} from subplot.conf text: KEY=value lines, # comments and blank lines ignored; refuses unknown, repeated,
    missing or malformed keys and any value outside its pattern (defaults filled in for optional keys)."""
    conf = {}
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.fullmatch(r"([A-Z_]{2,20})=(.*)", line)
        if not m:
            raise Refused(f"line {n}: expected KEY=value")
        key, value = m.groups()
        if key not in PATTERNS:
            raise Refused(f"line {n}: unknown key {key}")
        if key in conf:
            raise Refused(f"line {n}: {key} given twice")
        conf[key] = value
    for key, (regex, required) in PATTERNS.items():
        value = conf.get(key, "")
        if value == "":
            if required:
                raise Refused(f"{key} is required")
            conf[key] = DEFAULTS[key]
            if conf[key] == "":
                continue
            value = conf[key]
        if not re.fullmatch(regex, value):
            raise Refused(f"{key}: value not allowed (see subplot.conf.example for the format)")
        if key in PATH_KEYS and not _path_ok(value):
            raise Refused(f"{key}: no '..' or '//' and not '/'")
        if key in ("MEDIA_ROOTS", "HIDE_PATHS") and not all(_path_ok(p) for p in value.split(" ")):
            raise Refused(f"{key}: no '..' or '//' and not '/'")
        if key == "PC_IP" and not all(0 <= int(o) <= 255 for o in value.split(".")):
            raise Refused("PC_IP: not an IPv4 address")
    return conf


def tokens(conf, run_group, run_home):
    """The values substituted into the templates (derived ones included)."""
    t = dict(conf)
    t["RUN_GROUP"], t["RUN_HOME"] = run_group, run_home
    t["SOCKET_DIR"] = os.path.dirname(conf["SOCKET"])
    t["PLEX_DB_DIR"] = os.path.dirname(conf["PLEX_DB"])
    t["AUTH_DIR"] = conf["STATE_DIR"] + "/auth" if conf["AUTH"] == "login" else ""   # empty: its BindPaths line is dropped
    for key in ("RUN_GROUP",):
        if not re.fullmatch(NAME, t[key]):
            raise Refused(f"{key}: value not allowed")
    if not (re.fullmatch(PATH, run_home) and _path_ok(run_home)):
        raise Refused("RUN_USER's home folder has characters the units cannot take")
    return t


def render(template, t):
    """The template with every @KEY@ replaced; a line with an empty token is dropped; a line with a REPEAT token
    (@MEDIA_ROOT@, @HIDE_PATH@) repeats once per item of its list (none when the list is empty). Refuses an unknown token,
    a leftover token, or a comment sharing a line with a value."""
    out = []
    for line in template.split("\n"):
        keys = re.findall(r"@([A-Z_]+)@", line)
        for k in keys:
            if k not in REPEAT and k not in t:
                raise Refused(f"template token @{k}@ has no value")
        if any(k not in REPEAT and t[k] == "" for k in keys):
            continue
        rep_keys = [k for k in keys if k in REPEAT]
        if len(set(rep_keys)) > 1:
            raise Refused("a template line may repeat over one list only")
        items = [x for x in t.get(REPEAT[rep_keys[0]], "").split(" ") if x] if rep_keys else [None]
        for item in items:
            s = line
            for k in keys:
                s = s.replace(f"@{k}@", item if k in REPEAT else t[k])
            if re.search(r"@[A-Z_]+@", s):                                   # (values cannot hold '@'; systemd's @groups stay)
                raise Refused("a rendered line still holds a token")
            if "=" in s and re.search(r"\s[#;]", s.split("=", 1)[1]) and not s.startswith(("Description=",)):
                raise Refused("a comment shares a line with a value")
            out.append(s)
    return "\n".join(out)


def templates():
    names = []
    for step in STEPS:
        names.append((f"{step}.service.in", f"{step}.service"))
    for step in TIMERS:
        names.append((f"{step}.timer.in", f"{step}.timer"))
    names.append(("place.path.in", "place.path"))
    return names


def render_all(t, dest):
    """Render every template into dest (a private folder); returns [(unit name, path)]."""
    written = []
    owners = {0, int(os.environ.get("SUDO_UID", os.getuid()))}
    for src, kind in templates():
        path = os.path.join(UNITS, src)
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_uid not in owners or st.st_mode & 0o002:
            raise Refused(f"units/{src} must be a regular file owned by root or you, not writable by others")
        with open(path, encoding="utf-8") as f:
            text = render(f.read(), t)
        check_rendered(f"{t['UNIT_PREFIX']}-{kind}", text, t)
        name = f"{t['UNIT_PREFIX']}-{kind}"
        path = os.path.join(dest, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        written.append((name, path))
    return written


def as_user(conf, argv):
    """argv run as RUN_USER when we are root (runuser, absolute path), else as ourselves (--check)."""
    return ["/usr/sbin/runuser", "-u", conf["RUN_USER"], "--", *argv] if os.geteuid() == 0 else argv


def host_checks(conf):
    """The checks that need the real machine. Returns (run_group, run_home) or raises Refused with the fix."""
    try:
        pw = pwd.getpwnam(conf["RUN_USER"])
    except KeyError:
        raise Refused(f"RUN_USER {conf['RUN_USER']} does not exist (create it first)")
    if pw.pw_uid == 0:
        raise Refused("RUN_USER must not be root")
    try:
        run_group = grp.getgrgid(pw.pw_gid).gr_name
        grp.getgrnam(conf["PROXY_GROUP"])
    except KeyError:
        raise Refused(f"PROXY_GROUP {conf['PROXY_GROUP']} does not exist (your reverse proxy's group, e.g. www-data)")
    if os.path.realpath(conf["CODE_DIR"]) != os.path.realpath(REPO):
        raise Refused(f"CODE_DIR must be this checkout: {REPO}")
    env = os.path.join(conf["CONFIG_DIR"], ".env")
    try:
        st = os.lstat(env)
    except OSError:
        raise Refused(f"{env} is missing: cp config/env.example {env} && chmod 600 {env} && chown {conf['RUN_USER']} {env}")
    if (st.st_mode & 0o777) != 0o600 or st.st_uid != pw.pw_uid or os.path.islink(env):
        raise Refused(f"{env} must be a regular file, mode 600, owned by {conf['RUN_USER']}")
    rules = os.path.join(conf["CONFIG_DIR"], "rules.json")
    try:
        with open(rules, encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError):
        raise Refused(f"{rules} is missing or not JSON: cp config/rules.example.json {rules} and edit it")
    for root in conf["MEDIA_ROOTS"].split(" "):
        if not os.path.isdir(root):
            raise Refused(f"media root {root} is not a folder")
    if not os.path.isfile(conf["PLEX_DB"]):
        raise Refused(f"PLEX_DB {conf['PLEX_DB']} is not a file (README: the Plex DB copy)")
    for step in ("scan", "find", "sync", "place", "gen", "server"):
        if not os.path.isfile(os.path.join(REPO, "subplot", f"{step}.py")):
            raise Refused(f"subplot/{step}.py is missing from the checkout")
    py = os.path.join(conf["VENV"], "bin", "python")
    if os.path.islink(py) or not os.path.isfile(py):
        raise Refused(f"{py} must be a real file: python3 -m venv --copies {conf['VENV']}")
    # the venv belongs to the service account: run its python as that account, never as root
    if subprocess.run(as_user(conf, [py, "-I", "-c", "import numpy, onnxruntime"]), capture_output=True, timeout=120).returncode != 0:
        raise Refused(f"{conf['VENV']} lacks numpy/onnxruntime: {py} -m pip install -r {REPO}/requirements-sync.txt")
    with open(os.path.join(REPO, "subplot", "models", "silero_vad.onnx"), "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != MODEL_SHA256:
            raise Refused("subplot/models/silero_vad.onnx does not match its pinned sha256")
    g = subprocess.run(as_user(conf, ["ssh", "-G", conf["PC_HOST"]]), capture_output=True, text=True, timeout=30)
    if g.returncode != 0:
        raise Refused(f"ssh -G {conf['PC_HOST']} failed as {conf['RUN_USER']}: {g.stderr.strip()[:200]}")
    hostname = next((l.split(" ", 1)[1] for l in g.stdout.splitlines() if l.startswith("hostname ")), "")
    if hostname != conf["PC_IP"]:
        raise Refused(f"ssh alias {conf['PC_HOST']} must point at {conf['PC_IP']} (HostName in {conf['RUN_USER']}'s ~/.ssh/config); "
                      "the gen sandbox only lets that address through")
    return run_group, pw.pw_dir


def verify(paths, check=False):
    """systemd-analyze verify on the rendered units. In --check mode a missing executable is tolerated (a dry run against a
    made-up config); a real install refuses it."""
    p = subprocess.run(["/usr/bin/systemd-analyze", "verify", *paths], capture_output=True, text=True, timeout=120)
    bad = [l for l in (p.stderr + p.stdout).splitlines() if l.strip() and "Unit is bound to inactive unit" not in l
           and not (check and "is not executable" in l)]
    if bad:
        raise Refused("systemd-analyze verify: " + "; ".join(bad[:5]))


SECURITY_KEYS = re.compile(r"(User|Group|DynamicUser|PermissionsStartOnly|AmbientCapabilities|CapabilityBoundingSet|NoNewPrivileges)=(.*)")


def check_rendered(name, text, t):
    """A tampered template must not be able to raise privileges: every service runs as RUN_USER/RUN_GROUP, keeps an empty
    capability set and NoNewPrivileges, and no Exec line uses the '+' or '!' (run as root) prefixes."""
    if not name.endswith(".service"):
        return
    seen = {}
    for line in text.split("\n"):
        m = SECURITY_KEYS.fullmatch(line.strip())
        if m:
            seen.setdefault(m.group(1), []).append(m.group(2))
        if re.match(r"Exec\w*=\s*[-@:]*[+!]", line.strip()):
            raise Refused(f"{name}: an Exec line asks for root")
    want = {"User": [t["RUN_USER"]], "Group": [t["RUN_GROUP"]], "CapabilityBoundingSet": [""], "NoNewPrivileges": ["yes"]}
    for key, value in want.items():
        if seen.get(key) != value:
            raise Refused(f"{name}: {key} must be exactly {value[0]!r}")
    for key in ("DynamicUser", "PermissionsStartOnly", "AmbientCapabilities"):
        if key in seen:
            raise Refused(f"{name}: {key} is not allowed")


def _open_parent(path):
    """An O_DIRECTORY|O_NOFOLLOW fd for the folder holding path, walked one component at a time from '/': no symlink anywhere
    on the way (the service account could swap one in its own home)."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in [p for p in os.path.dirname(path).split("/") if p]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise Refused(f"{path}: a parent folder is missing or is a symlink ({exc.strerror})")
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _own_dir(parent_fd, name, uid, gid, mode, label):
    """Create name inside parent_fd if missing, or take it over only if the service account already owns it (never a
    system folder that exists already, never through a symlink). chown/chmod act on the opened fd. Returns the fd."""
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise Refused(f"{label} is not a plain folder ({exc.strerror})")
    st = os.fstat(fd)
    if not created and st.st_uid != uid:
        os.close(fd)
        raise Refused(f"{label} already exists and is not owned by the service account; pick a new folder")
    os.fchown(fd, uid, gid)
    os.fchmod(fd, mode)
    return fd


def install(conf, written, run_group):
    pw = pwd.getpwnam(conf["RUN_USER"])
    parent = _open_parent(conf["STATE_DIR"])
    try:
        state_fd = _own_dir(parent, os.path.basename(conf["STATE_DIR"]), pw.pw_uid, pw.pw_gid, 0o700, "STATE_DIR")
    finally:
        os.close(parent)
    try:
        for sub in STATE_SUBDIRS + (("auth",) if conf["AUTH"] == "login" else ()):
            os.close(_own_dir(state_fd, sub, pw.pw_uid, pw.pw_gid, 0o700, f"STATE_DIR/{sub}"))
    finally:
        os.close(state_fd)
    sock_dir = os.path.dirname(conf["SOCKET"])
    if sock_dir == "/" or not _path_ok(sock_dir):
        raise Refused("SOCKET must live in a folder of its own, e.g. /var/lib/subplot-sock/board.sock")
    parent = _open_parent(sock_dir)
    try:
        # setgid: the socket the board creates inherits the proxy's group (the board itself may not chown: syscall filter)
        os.close(_own_dir(parent, os.path.basename(sock_dir), pw.pw_uid, grp.getgrnam(conf["PROXY_GROUP"]).gr_gid, 0o2750, "the socket's folder"))
    finally:
        os.close(parent)
    # units: all new files written next to their targets first, then renamed into place; any failure puts the old ones back
    staged, old = [], {}
    try:
        for name, path in written:
            target = os.path.join(SYSTEMD, name)
            try:
                st = os.lstat(target)
                if not stat.S_ISREG(st.st_mode):
                    raise Refused(f"{target} is not a regular file; remove it by hand first")
                with open(target, "rb") as f:
                    old[target] = f.read()
            except FileNotFoundError:
                old[target] = None
            tmp = target + ".subplot-new"
            subprocess.run(["/usr/bin/install", "-o", "root", "-g", "root", "-m", "644", "-T", path, tmp], check=True)
            staged.append((tmp, target))
        for tmp, target in staged:
            os.rename(tmp, target)
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
    except BaseException:
        for tmp, target in staged:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            if target in old:
                if old[target] is None:
                    try:
                        os.unlink(target)
                    except FileNotFoundError:
                        pass
                else:
                    with open(target, "wb") as f:
                        f.write(old[target])
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=False)
        raise
    p = conf["UNIT_PREFIX"]
    subprocess.run(["/usr/bin/systemctl", "enable", "--now", *[f"{p}-{s}.timer" for s in TIMERS], f"{p}-place.path"], check=True)
    subprocess.run(["/usr/bin/systemctl", "enable", f"{p}-board.service"], check=True)
    subprocess.run(["/usr/bin/systemctl", "restart", f"{p}-board.service"], check=True)


def _cleanup(tmp):
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    conf_path = args[0] if args else DEFAULT_CONF
    if not check and os.geteuid() != 0:
        print("run with sudo (or use --check)", file=sys.stderr)
        return 2
    try:
        with open(conf_path, encoding="utf-8") as f:
            text = f.read(64 * 1024 + 1)
        if len(text) > 64 * 1024:
            raise Refused("the settings file is over 64 KiB")
        conf = parse_conf(text)
        if check:
            try:
                run_group, run_home = host_checks(conf)
            except Refused as exc:
                print(f"host check: {exc}", file=sys.stderr)
                run_group, run_home = conf["RUN_USER"], f"/home/{conf['RUN_USER']}"
        else:
            run_group, run_home = host_checks(conf)
        t = tokens(conf, run_group, run_home)
        tmp = tempfile.mkdtemp(prefix="subplot-units-")
        written = render_all(t, tmp)
        verify([p for _, p in written], check)
        if check:
            print("rendered and verified (nothing installed):")
            for name, path in written:
                print(f"  {path}")
            return 0
        install(conf, written, run_group)
        shutil.rmtree(tmp, ignore_errors=True)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        _cleanup(locals().get("tmp"))
        return 2
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"refused: {getattr(exc, 'strerror', None) or exc}", file=sys.stderr)
        _cleanup(locals().get("tmp"))
        return 2
    print("installed: " + " ".join(n for n, _ in written))
    if conf["AUTH"] == "login":
        print("next: point your reverse proxy at the socket (it must set X-Forwarded-Proto and X-Real-IP), then get the")
        print("setup code (each run makes a new one; it works once) and create the account on the page:")
        print(f"  sudo runuser -u {conf['RUN_USER']} -- env SUBS_STATE={conf['STATE_DIR']} python3 -I {conf['CODE_DIR']}/subplot/auth.py setup-code")
    else:
        print("next: point your reverse proxy at the socket and sign in through it (README, 'Reverse proxy').")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
