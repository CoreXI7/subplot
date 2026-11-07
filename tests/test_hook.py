import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO_ROOT, "tools", "pre-push")

TOKEN = "zzqq-secret-7"  # privacy-ok: fake value the hook must find in test repos
PATTERN = r"zzqq-secret-7"
ZERO_SHA = "0" * 40


class HookTestCase(unittest.TestCase):
    def setUp(self):
        if not os.path.isfile(HOOK):
            self.skipTest("hook not found at %s" % HOOK)
        sandbox = os.environ.get("SUBS_SANDBOX")
        if sandbox:
            os.makedirs(sandbox, exist_ok=True)
        self.tmpdir = tempfile.mkdtemp(prefix="hook-test-", dir=sandbox)
        self.repo = os.path.join(self.tmpdir, "repo")
        os.makedirs(self.repo)
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "Test Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Test Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
            }
        )
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def git(self, *args, env=None):
        child_env = self.env.copy()
        if env:
            child_env.update(env)
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            self.fail("git %s failed (%d): %s" % (" ".join(args), proc.returncode, proc.stderr))
        return proc

    def write(self, relpath, data):
        path = os.path.join(self.repo, relpath)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if isinstance(data, bytes):
            with open(path, "wb") as handle:
                handle.write(data)
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)

    def commit(self, message="clean commit", env=None):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, env=env)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def set_private(self, lines):
        path = os.path.join(self.repo, ".git", "info", "private-patterns")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")

    def run_hook(self, *args, stdin=None):
        command = [sys.executable, HOOK, *args]
        if stdin is None:
            return subprocess.run(
                command,
                cwd=self.repo,
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        return subprocess.run(
            command,
            cwd=self.repo,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def findings(self, proc):
        return (proc.stdout or "") + (proc.stderr or "")

    def assertFinding(self, proc, where, pattern="private-1"):
        output = self.findings(proc)
        for line in output.splitlines():
            if where in line and pattern in line:
                return
        self.fail("no line containing %r and %r in output:\n%s" % (where, pattern, output))

    def assertNoOutput(self, proc):
        self.assertEqual((proc.stdout or "").strip(), "", self.findings(proc))
        self.assertEqual((proc.stderr or "").strip(), "", self.findings(proc))

    def test_clean_repo_passes_scan_and_history(self):
        self.write("README.md", "hello\n")
        self.commit("initial clean commit")
        for args in (("--scan",), ("--history",)):
            proc = self.run_hook(*args)
            self.assertEqual(proc.returncode, 0, self.findings(proc))
            self.assertNoOutput(proc)

    def test_private_pattern_in_file_content(self):
        self.set_private([PATTERN])
        self.write("notes.txt", "prefix %s suffix\n" % TOKEN)
        self.commit()
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "notes.txt")
        self.assertNotIn(TOKEN, self.findings(proc))

    def test_private_pattern_in_file_path(self):
        self.set_private([PATTERN])
        self.write(TOKEN + ".txt", "clean\n")
        self.commit()
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, TOKEN + ".txt")

    def test_private_pattern_in_commit_message(self):
        self.set_private([PATTERN])
        self.write("a.txt", "clean\n")
        self.commit("add %s to repo" % TOKEN)
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "message")
        self.assertNotIn(TOKEN, self.findings(proc))

    def test_private_pattern_in_author_name(self):
        self.set_private([PATTERN])
        self.write("a.txt", "clean\n")
        self.commit("clean", env={"GIT_AUTHOR_NAME": TOKEN + " Person"})
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "author")
        self.assertNotIn(TOKEN, self.findings(proc))

    def test_private_pattern_in_committer_email(self):
        self.set_private([PATTERN])
        self.write("a.txt", "clean\n")
        self.commit("clean", env={"GIT_COMMITTER_EMAIL": TOKEN + "@example.com"})
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "committer")
        self.assertNotIn(TOKEN, self.findings(proc))

    def test_private_pattern_in_binary_file(self):
        self.set_private([PATTERN])
        self.write("blob.bin", b"\x00\x00" + TOKEN.encode("ascii") + b"\x00\x00")
        self.commit()
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "blob.bin")
        self.assertNotIn(TOKEN, self.findings(proc))

    def test_deleted_file_found_in_history_but_not_scan(self):
        self.set_private([PATTERN])
        self.write("gone.txt", TOKEN + "\n")
        self.commit("add gone.txt")
        self.git("rm", "-q", "gone.txt")
        self.commit("remove gone.txt")
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "gone.txt")
        scan = self.run_hook("--scan")
        self.assertEqual(scan.returncode, 0, self.findings(scan))
        self.assertNoOutput(scan)

    def test_renamed_file_old_path_found(self):
        self.set_private([PATTERN])
        old = TOKEN + "-old.txt"
        self.write(old, "clean\n")
        self.commit("add old name")
        self.git("mv", old, "renamed.txt")
        self.commit("rename file")
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, old)

    def test_forbidden_files_found_by_scan(self):
        self.write(".env", "CLEAN=1\n")
        self.write("rules.json", "{}\n")
        self.write("notes.bak", "clean\n")
        self.write("rules.example.json", "{}\n")
        self.commit()
        proc = self.run_hook("--scan")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        for where in (".env", "rules.json", "notes.bak"):
            self.assertFinding(proc, where, "forbidden-file")
        self.assertNotIn("rules.example.json", self.findings(proc))

    def test_lan_ip_examples(self):
        self.write("docs.txt", "use 10.0.0.50 and 192.168.0.0/16 as examples\n")  # privacy-ok: test data for the hook
        self.commit()
        proc = self.run_hook("--scan")
        self.assertEqual(proc.returncode, 0, self.findings(proc))
        self.assertNoOutput(proc)
        self.write("leak.txt", "host 192.168.5.5\n")  # privacy-ok: test data for the hook
        self.commit()
        proc = self.run_hook("--scan")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "leak.txt", "lan-ip")

    def test_personal_mail_and_noreply(self):
        self.write("noreply.txt", "contact 1234+octocat@users.noreply.github.com\n")
        self.commit()
        proc = self.run_hook("--scan")
        self.assertEqual(proc.returncode, 0, self.findings(proc))
        self.assertNoOutput(proc)
        self.write("mail.txt", "write someone@gmail.com\n")  # privacy-ok: test data for the hook
        self.commit()
        proc = self.run_hook("--scan")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "mail.txt", "personal-mail")
        self.assertNotIn("someone@gmail.com", self.findings(proc))  # privacy-ok: test data for the hook

    def test_privacy_ok_line_is_skipped(self):
        self.set_private([PATTERN])
        self.write("skipped.txt", TOKEN + " privacy-ok\n")
        self.write("found.txt", TOKEN + "\n")
        self.commit()
        proc = self.run_hook("--scan")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "found.txt")
        self.assertNotIn("skipped.txt", self.findings(proc))

    def test_output_never_contains_matched_text(self):
        self.set_private([PATTERN])
        self.write("content.txt", "line with %s inside\n" % TOKEN)
        self.commit("message also has %s" % TOKEN)
        proc = self.run_hook("--history")
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertNotIn(TOKEN, self.findings(proc))

    def test_invalid_private_regex_exit_2(self):
        self.set_private(["valid-[0-9]+", "("])
        self.write("a.txt", "clean\n")
        self.commit()
        proc = self.run_hook("--scan")
        self.assertEqual(proc.returncode, 2, self.findings(proc))
        self.assertIn("2", self.findings(proc))

    def test_pre_push_new_branch_scans_commits(self):
        self.set_private([PATTERN])
        self.write("a.txt", "clean\n")
        sha = self.commit("branch with %s" % TOKEN)
        stdin = "refs/heads/main %s refs/heads/main %s\n" % (sha, ZERO_SHA)
        proc = self.run_hook(stdin=stdin)
        self.assertEqual(proc.returncode, 1, self.findings(proc))
        self.assertFinding(proc, "message")
        self.assertNotIn(TOKEN, self.findings(proc))


if __name__ == "__main__":
    unittest.main()
