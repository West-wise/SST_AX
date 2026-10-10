"""Offline Git fixtures for bounded, immutable SSTC publication candidates."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sstc_candidate as candidate


class SstcCandidateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "SSTC"
        self.repository.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write("app/src/main/java/example/Existing.kt", "base\n")
        self.write("app/src/main/java/example/Removed.kt", "remove\n")
        self.write("README.md", "unchanged\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.revision = self.git("rev-parse", "HEAD").decode().strip()
        self.branch = "ax/sstc-sync/candidate-test"
        self.git("checkout", "-qb", self.branch)

    def git(self, *arguments):
        environment = dict(os.environ)
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_TERMINAL_PROMPT="0")
        result = subprocess.run(["git", *arguments], cwd=self.repository, env=environment,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout

    def write(self, name, content):
        path = self.repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        return path

    def snapshot(self):
        return candidate.candidate_snapshot(self.repository, self.revision, self.branch)

    def test_snapshot_includes_unstaged_staged_untracked_and_deleted_files(self):
        self.write("app/src/main/java/example/Existing.kt", "changed\n")
        self.write("app/src/main/res/layout/added.xml", "<FrameLayout/>\n")
        self.write("app/src/main/java/example/Staged.java", "class Staged {}\n")
        self.git("add", "app/src/main/java/example/Staged.java")
        (self.repository / "app/src/main/java/example/Removed.kt").unlink()
        result = self.snapshot()
        self.assertEqual([item["path"] for item in result], sorted([
            "app/src/main/java/example/Existing.kt", "app/src/main/java/example/Removed.kt",
            "app/src/main/java/example/Staged.java", "app/src/main/res/layout/added.xml"]))
        by_path = {item["path"]: item for item in result}
        self.assertIsNone(by_path["app/src/main/java/example/Removed.kt"]["sha256"])
        self.assertEqual(by_path["app/src/main/java/example/Existing.kt"]["sha256"],
                         hashlib.sha256(b"changed\n").hexdigest())
        self.assertTrue(all(item["mode"] == "100644" for item in result))


    def test_snapshot_batches_all_modes_into_one_git_process(self):
        self.write("app/src/main/java/example/Existing.kt", "changed\n")
        self.write("app/src/main/java/example/Removed.kt", "changed\n")
        for number in range(4):
            self.write(f"app/src/main/java/example/Added{number}.kt", "new\n")
        with patch.object(candidate, "git", wraps=candidate.git) as calls:
            result = self.snapshot()
        self.assertEqual(len(result), 6)
        self.assertEqual(calls.call_count, 6)
        commands = [call.args[1] for call in calls.call_args_list]
        mode_commands = [command for command in commands if command[0] == "ls-tree"]
        self.assertEqual(mode_commands, [["ls-tree", "-z", self.revision, "--",
                                         *[item["path"] for item in result]]])

    def test_malformed_batched_modes_are_rejected(self):
        name = "app/src/main/java/example/Existing.kt"
        self.write(name, "changed\n")
        valid = b"100644 blob " + b"a" * 40 + b"\t" + name.encode() + b"\0"
        cases = (
            (valid[:-1], "CANDIDATE_TREE_FORMAT"),
            (valid + valid, "CANDIDATE_TREE_FORMAT"),
            (valid.replace(name.encode(), b"README.md"), "CANDIDATE_TREE_FORMAT"),
            (valid.replace(b"blob", b"tree"), "CANDIDATE_TREE_FORMAT"),
            (valid.replace(b"a" * 40, b"a" * 39), "CANDIDATE_TREE_FORMAT"),
            (valid.replace(b"a" * 40, b"g" * 40), "CANDIDATE_TREE_FORMAT"),
            (valid.replace(b"100644", b"100755"), "CANDIDATE_FILE_MODE_REFUSED"),
            (valid.replace(b"100644", b"120000"), "CANDIDATE_FILE_MODE_REFUSED"),
            (valid.replace(b"100644 blob", b"100644  blob"), "CANDIDATE_TREE_FORMAT"),
            (b"\0", "CANDIDATE_TREE_FORMAT"),
            (b"100644 blob " + b"a" * 40 + b"\t\xff\0", "CANDIDATE_TREE_FORMAT"),
        )
        real_git = candidate.git
        for raw, reason in cases:
            def malformed(path, arguments, **kwargs):
                if arguments[0] == "ls-tree":
                    return raw
                return real_git(path, arguments, **kwargs)
            with self.subTest(raw=raw), patch.object(candidate, "git", side_effect=malformed):
                with self.assertRaisesRegex(ValueError, "^" + reason + "$"):
                    self.snapshot()

    def test_private_tree_rechecks_content_after_tree_creation(self):
        path = self.write("app/src/main/java/example/Existing.kt", "approved\n")
        files = self.snapshot()
        real_git = candidate.git
        def change_after_tree(repository, arguments, **kwargs):
            result = real_git(repository, arguments, **kwargs)
            if arguments[0] == "write-tree":
                path.write_bytes(b"changed after tree creation\n")
            return result
        with patch.object(candidate, "git", side_effect=change_after_tree):
            with self.assertRaisesRegex(ValueError, "^CANDIDATE_CHANGED$"):
                candidate.tree_payload(self.repository, self.revision, self.branch, files)


    def test_private_tree_includes_added_and_deleted_bytes_without_changing_index(self):
        self.write("app/src/main/java/example/Existing.kt", "staged\n")
        self.git("add", "app/src/main/java/example/Existing.kt")
        self.write("app/src/main/java/example/Existing.kt", "unstaged\n")
        self.write("app/src/main/java/example/Added.kt", "added\n")
        (self.repository / "app/src/main/java/example/Removed.kt").unlink()
        status = self.git("status", "--porcelain=v1", "-z")
        index = (self.repository / ".git/index").read_bytes()
        tree, entries = candidate.tree_payload(self.repository, self.revision, self.branch, self.snapshot())
        self.assertEqual(len(tree), 40)
        self.assertEqual(self.git("show", tree + ":app/src/main/java/example/Existing.kt"), b"unstaged\n")
        self.assertEqual(self.git("show", tree + ":app/src/main/java/example/Added.kt"), b"added\n")
        self.assertNotIn("app/src/main/java/example/Removed.kt",
                         self.git("ls-tree", "-r", "--name-only", tree).decode())
        self.assertEqual(self.git("show", tree + ":README.md"), b"unchanged\n")
        self.assertEqual(index, (self.repository / ".git/index").read_bytes())
        self.assertEqual(status, self.git("status", "--porcelain=v1", "-z"))
        removed = next(entry for entry in entries if entry["path"].endswith("Removed.kt"))
        self.assertIsNone(removed["sha"])
        added = next(entry for entry in entries if entry["path"].endswith("Added.kt"))
        self.assertEqual(added["content"], "added\n")

    def test_bytes_or_changed_file_set_after_snapshot_reject_publication(self):
        path = self.write("app/src/main/java/example/Existing.kt", "approved\n")
        files = self.snapshot()
        path.write_bytes(b"changed later\n")
        with self.assertRaisesRegex(ValueError, "CANDIDATE_CHANGED"):
            candidate.tree_payload(self.repository, self.revision, self.branch, files)
        path.write_bytes(b"approved\n")
        self.write("app/src/main/java/example/Later.kt", "new file\n")
        with self.assertRaisesRegex(ValueError, "CANDIDATE_CHANGED"):
            candidate.tree_payload(self.repository, self.revision, self.branch, files)

    def test_replace_objects_cannot_change_pinned_source_tree(self):
        self.write("README.md", "replacement tree\n")
        self.git("add", ".")
        self.git("commit", "-qm", "replacement")
        replacement = self.git("rev-parse", "HEAD").decode().strip()
        self.git("reset", "--hard", self.revision)
        self.git("replace", self.revision, replacement)
        self.assertEqual(self.git("show", self.revision + ":README.md"), b"replacement tree\n")
        self.write("app/src/main/java/example/Existing.kt", "approved change\n")
        files = self.snapshot()
        self.assertEqual([item["path"] for item in files], ["app/src/main/java/example/Existing.kt"])
        tree, _ = candidate.tree_payload(self.repository, self.revision, self.branch, files)
        self.assertEqual(self.git("--no-replace-objects", "show", tree + ":README.md"), b"unchanged\n")

    def test_repository_diff_and_textconv_drivers_are_never_executed(self):
        marker = self.root / "driver-ran"
        script = self.root / "diff-driver.py"
        script.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('ran')\n",
                          encoding="utf-8")
        command = '"' + sys.executable + '" "' + str(script) + '"'
        (self.repository / ".git/info/attributes").write_text("*.kt diff=trap\n", encoding="utf-8")
        self.git("config", "diff.trap.command", command)
        self.git("config", "diff.trap.textconv", command)
        self.write("app/src/main/java/example/Existing.kt", "changed\n")
        output = candidate.git(self.repository, ["diff", self.revision, "--"])
        self.assertIn(b"changed", output)
        self.assertFalse(marker.exists())

    def test_staged_rename_publishes_old_deletion_and_new_content(self):
        old_name = "app/src/main/java/example/Existing.kt"
        new_name = "app/src/main/java/example/Renamed.kt"
        self.git("mv", old_name, new_name)
        index = (self.repository / ".git/index").read_bytes()
        files = self.snapshot()
        self.assertEqual([item["path"] for item in files], [old_name, new_name])
        self.assertIsNone(files[0]["sha256"])
        self.assertEqual(files[1]["sha256"], hashlib.sha256(b"base\n").hexdigest())
        tree, entries = candidate.tree_payload(self.repository, self.revision, self.branch, files)
        self.assertNotIn(old_name, self.git("ls-tree", "-r", "--name-only", tree).decode())
        self.assertEqual(self.git("show", tree + ":" + new_name), b"base\n")
        self.assertIsNone(entries[0]["sha"])
        self.assertEqual(entries[1]["content"], "base\n")
        self.assertEqual(index, (self.repository / ".git/index").read_bytes())

    def test_branch_and_head_are_pinned(self):
        self.write("app/src/main/java/example/Existing.kt", "changed\n")
        for revision, branch in (("f" * 40, self.branch), (self.revision, "main"),
                                 (self.revision, "ax/sstc-sync/another")):
            with self.subTest(revision=revision, branch=branch), self.assertRaises(ValueError):
                candidate.candidate_snapshot(self.repository, revision, branch)
        self.git("add", ".")
        self.git("commit", "-qm", "unexpected commit")
        with self.assertRaisesRegex(ValueError, "CANDIDATE_HEAD_OR_BRANCH_CHANGED"):
            self.snapshot()

    def test_empty_or_more_than_twenty_changed_files_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "CANDIDATE_CHANGE_COUNT"):
            self.snapshot()
        for number in range(21):
            self.write("app/src/main/java/example/File" + str(number) + ".kt", "data\n")
        with self.assertRaisesRegex(ValueError, "CANDIDATE_CHANGE_COUNT"):
            self.snapshot()

    def test_android_manifest_workflows_build_config_and_hidden_paths_are_rejected(self):
        names = ("app/src/main/AndroidManifest.xml", ".github/workflows/build.yml",
                 "app/build.gradle.kts", "app/src/main/.hidden.kt", "README.md")
        for name in names:
            with self.subTest(path=name):
                path = self.write(name, "changed\n")
                with self.assertRaises(ValueError):
                    self.snapshot()
                if name == "README.md":
                    path.write_bytes(b"unchanged\n")
                else:
                    path.unlink()

    def test_secrets_binary_and_invalid_utf8_are_rejected(self):
        for content in (b"xoxb-" + b"a" * 24, b"hello\0world", b"\xff\xfe\x01"):
            with self.subTest(content=content):
                self.write("app/src/main/java/example/Existing.kt", content)
                with self.assertRaises(ValueError):
                    self.snapshot()

    def test_file_and_total_size_limits_are_enforced(self):
        path = self.write("app/src/main/java/example/Existing.kt", b"a" * 65537)
        with self.assertRaisesRegex(ValueError, "CANDIDATE_SIZE_LIMIT"):
            self.snapshot()
        path.write_bytes(b"base\n")
        for number in range(17):
            self.write("app/src/main/java/example/Large" + str(number) + ".kt", b"a" * 65536)
        with self.assertRaisesRegex(ValueError, "CANDIDATE_SIZE_LIMIT"):
            self.snapshot()

    def test_tracked_symlink_mode_is_rejected_without_following_target(self):
        blob = self.git("hash-object", "-w", "--stdin").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "120000", blob,
                 "app/src/main/java/example/Link.kt")
        self.git("commit", "-qm", "synthetic symlink object")
        self.revision = self.git("rev-parse", "HEAD").decode().strip()
        self.write("app/src/main/java/example/Link.kt", "replacement\n")
        with self.assertRaisesRegex(ValueError, "CANDIDATE_FILE_MODE_REFUSED"):
            self.snapshot()


if __name__ == "__main__":
    unittest.main()
