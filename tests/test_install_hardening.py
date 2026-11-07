"""Regression tests for the installer review (root code): path normalisation, no-follow folder creation that never takes
over a folder someone else owns, and rendered units that cannot ask for root. Runs without root."""
import os, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import install as I  # noqa: E402

BASE = tempfile.mkdtemp(prefix="subplot-inst-", dir=os.environ.get("SUBS_SANDBOX") or None)
UID, GID = os.getuid(), os.getgid()
T = {"RUN_USER": "media", "RUN_GROUP": "media"}
GOOD = "[Service]\nUser=media\nGroup=media\nExecStart=/usr/bin/python3 -I /x.py\nNoNewPrivileges=yes\nCapabilityBoundingSet=\n"


class PathRules(unittest.TestCase):
    def test_non_normal_paths_refused(self):
        for bad in ("/", "/.", "/a/./b", "/a/../b", "/a//b", "/a/"):
            self.assertFalse(I._path_ok(bad), bad)
        self.assertTrue(I._path_ok("/var/lib/subplot"))


class OwnDir(unittest.TestCase):
    def test_creates_and_reuses_own_folder(self):
        path = os.path.join(BASE, "state")
        for _ in range(2):                                         # a second run takes over the folder it made
            parent = I._open_parent(path)
            try:
                os.close(I._own_dir(parent, "state", UID, GID, 0o700, "STATE_DIR"))
            finally:
                os.close(parent)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)

    def test_symlink_in_parent_chain_refused(self):
        real = os.path.join(BASE, "real"); os.makedirs(real, exist_ok=True)
        link = os.path.join(BASE, "link")
        if not os.path.lexists(link):
            os.symlink(real, link)
        with self.assertRaises(I.Refused):
            I._open_parent(os.path.join(link, "state"))

    def test_symlinked_target_refused(self):
        os.makedirs(os.path.join(BASE, "elsewhere"), exist_ok=True)
        if not os.path.lexists(os.path.join(BASE, "st2")):
            os.symlink(os.path.join(BASE, "elsewhere"), os.path.join(BASE, "st2"))
        parent = I._open_parent(os.path.join(BASE, "st2"))
        try:
            with self.assertRaises(I.Refused):
                I._own_dir(parent, "st2", UID, GID, 0o700, "STATE_DIR")
        finally:
            os.close(parent)

    def test_existing_folder_owned_by_someone_else_refused(self):
        parent = I._open_parent("/etc")                             # '/', which holds etc (root's)
        try:
            with self.assertRaises(I.Refused):
                I._own_dir(parent, "etc", UID, GID, 0o700, "STATE_DIR")
        finally:
            os.close(parent)


class RenderedUnits(unittest.TestCase):
    def test_good_unit_passes(self):
        I.check_rendered("x-board.service", GOOD, T)
        I.check_rendered("x-scan.timer", "[Timer]\nOnCalendar=daily\n", T)

    def test_privilege_raising_units_refused(self):
        for bad in (GOOD.replace("User=media", "User=root"), GOOD.replace("User=media\n", ""),
                    GOOD + "User=root\n", GOOD.replace("Group=media", "Group=root"),
                    GOOD.replace("ExecStart=", "ExecStart=+"), GOOD + "ExecStartPre=!/bin/true\n",
                    GOOD + "ExecStartPost=-+/bin/true\n", GOOD.replace("CapabilityBoundingSet=", "CapabilityBoundingSet=CAP_SYS_ADMIN"),
                    GOOD.replace("NoNewPrivileges=yes", "NoNewPrivileges=no"), GOOD + "AmbientCapabilities=CAP_CHOWN\n",
                    GOOD + "PermissionsStartOnly=yes\n", GOOD + "DynamicUser=yes\n"):
            with self.assertRaises(I.Refused, msg=bad):
                I.check_rendered("x-board.service", bad, T)

    def test_shipped_templates_pass_the_check(self):
        conf = I.parse_conf(open(os.path.join(ROOT, "config", "subplot.conf.example")).read())
        t = I.tokens(conf, "media", "/home/media")
        out = tempfile.mkdtemp(dir=BASE)
        self.assertEqual(len(I.render_all(t, out)), 12)

    def test_tampered_template_refused_by_render_all(self):
        import shutil
        units = tempfile.mkdtemp(dir=BASE)
        for name in os.listdir(I.UNITS):
            shutil.copy(os.path.join(I.UNITS, name), units)
        board = os.path.join(units, "board.service.in")
        with open(board) as f:
            text = f.read()
        with open(board, "w") as f:
            f.write(text.replace("User=@RUN_USER@", "User=root"))
        conf = I.parse_conf(open(os.path.join(ROOT, "config", "subplot.conf.example")).read())
        saved, I.UNITS = I.UNITS, units
        try:
            with self.assertRaises(I.Refused):
                I.render_all(I.tokens(conf, "media", "/home/media"), tempfile.mkdtemp(dir=BASE))
        finally:
            I.UNITS = saved

    def test_world_writable_template_refused(self):
        src = os.path.join(I.UNITS, "scan.timer.in")
        mode = os.stat(src).st_mode & 0o777
        try:
            os.chmod(src, mode | 0o002)
            conf = I.parse_conf(open(os.path.join(ROOT, "config", "subplot.conf.example")).read())
            with self.assertRaises(I.Refused):
                I.render_all(I.tokens(conf, "media", "/home/media"), tempfile.mkdtemp(dir=BASE))
        finally:
            os.chmod(src, mode)


if __name__ == "__main__":
    unittest.main()
