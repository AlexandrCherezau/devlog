import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_updates.py"


def git(cwd, *args, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError("git %s failed: %s" % (args[0], result.stderr))
    return result


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.remote = self.base / "origin.git"
        self.repo = self.base / "repo"
        git(self.base, "init", "--bare", str(self.remote))
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        self.write("seed.txt", "repository seed\n")
        self.commit("baseline")
        git(self.repo, "push", "-u", "origin", "main")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, content):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", message)
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def publish(self, *args, cwd=None):
        return subprocess.run(
            ["python3", str(SCRIPT), *args], cwd=cwd or self.repo, text=True, capture_output=True
        )

    def remote_tree(self, path):
        return git(self.repo, "--git-dir", str(self.remote), "show", "updates:" + path, check=False)

    def remote_files(self):
        result = git(
            self.repo, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "updates"
        )
        return result.stdout.splitlines()

    def test_new_branch_baselines_first_source_commit_then_publishes_history(self):
        first = self.repo / "devlog.md"
        first.write_text("## 2026-01-01 — Первое\n- исходное\n", encoding="utf-8")
        first_sha = self.commit("first source entry")
        self.write("devlog.md", "## 2026-01-01 — Первое\n- исходное\n- второе\n")
        second_sha = self.commit("second source entry")
        self.write(
            "devlog.md",
            "## 2026-01-01 — Первое\n- исходное\n- второе\n\n## 2026-01-02 — Новое\n- целиком\n",
        )
        third_sha = self.commit("third source entry")

        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        files = [name for name in self.remote_files() if name.startswith("updates/")]
        self.assertEqual(len(files), 2)
        self.assertTrue(any(second_sha[:12] in name for name in files))
        self.assertTrue(any(third_sha[:12] in name for name in files))
        self.assertFalse(any(first_sha[:12] in name for name in files))
        self.assertIn("+ - второе", "\n".join(self.remote_tree(name).stdout for name in files))
        self.assertIn("Новое", "\n".join(self.remote_tree(name).stdout for name in files))

    def test_json_record_renders_and_repeat_run_is_idempotent(self):
        record = {
            "version": 1,
            "id": "change-1",
            "created_at": "2026-10-01T02:30:00+05:00",
            "project": "eldskills",
            "title": "Новая профессия",
            "changes": ["Добавлен класс"],
            "status": "tested",
            "verification": ["Пройден сценарий"],
            "notes": ["Доступно всем"],
            "source_revision": "a" * 40,
        }
        self.write(
            "entries/2026-09-30/change-1.json", json.dumps(record, ensure_ascii=False) + "\n"
        )
        self.write("devlog.md", "## 2026-10-01 — Baseline\n- legacy baseline\n")
        first_sha = self.commit("first source record and legacy baseline")
        record["id"] = "change-2"
        record["changes"].append("Добавлен новый уровень")
        self.write(
            "entries/2026-09-30/change-2.json", json.dumps(record, ensure_ascii=False) + "\n"
        )
        sha = self.commit("add next immutable record")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        files = [name for name in self.remote_files() if name.startswith("updates/")]
        self.assertEqual(len(files), 2)
        self.assertTrue(any(first_sha[:12] in name for name in files))
        self.assertTrue(any(sha[:12] in name for name in files))
        body = "\n".join(self.remote_tree(name).stdout for name in files)
        for value in (
            "Новая профессия",
            "Добавлен класс",
            "Добавлен новый уровень",
            "Пройден сценарий",
            "Доступно всем",
            "a" * 40,
        ):
            self.assertIn(value, body)
        tip = git(self.repo, "--git-dir", str(self.remote), "rev-parse", "updates").stdout
        again = self.publish()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(
            tip, git(self.repo, "--git-dir", str(self.remote), "rev-parse", "updates").stdout
        )
        self.assertEqual(len([n for n in self.remote_files() if n.startswith("updates/")]), 2)

    def test_failed_push_recovers_without_duplicate_publication(self):
        self.write("devlog.md", "## 2026-01-01 — запись\n- исходный факт\n")
        self.commit("first devlog source commit")
        self.write(
            "devlog.md",
            "## 2026-01-01 — запись\n- исходный факт\n- опубликовано после восстановления\n",
        )
        sha = self.commit("source change")
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        failed = self.publish()
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn("origin.git", failed.stderr)
        self.assertNotIn("credentials", failed.stderr)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")
        git_dir = Path(git(self.repo, "rev-parse", "--git-dir").stdout.strip())
        if not git_dir.is_absolute():
            git_dir = self.repo / git_dir
        self.assertEqual(list(git_dir.glob("devlog-updates-*")), [])
        hook.unlink()
        recovered = self.publish()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(len([name for name in self.remote_files() if sha[:12] in name]), 1)

    def test_existing_json_entry_cannot_be_modified_or_deleted(self):
        record = {
            "version": 1,
            "id": "fixed-entry",
            "created_at": "2026-10-01T12:30:00+05:00",
            "project": "eldskills",
            "title": "Неизменяемая запись",
            "changes": ["Сохранено"],
            "status": "implemented",
            "verification": [],
            "notes": [],
        }
        path = "entries/2026-10-01/fixed-entry.json"
        self.write(path, json.dumps(record, ensure_ascii=False) + "\n")
        self.commit("first entry baseline")
        record["title"] = "Изменённая запись"
        self.write(path, json.dumps(record, ensure_ascii=False) + "\n")
        self.commit("attempt to rewrite entry")
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("неизменяемы", result.stderr)
        self.assertEqual(
            git(
                self.repo, "--git-dir", str(self.remote), "for-each-ref", "refs/heads/updates"
            ).stdout,
            "",
        )

    def test_deletion_and_correction_are_reported(self):
        self.write(
            "devlog.md",
            "## 2026-01-01 — Раздел\n- старый факт\n- удаляемый факт\n\n## 2026-01-02 — Удалённый\n- устарело\n",
        )
        self.commit("add section")
        self.publish()
        self.write("devlog.md", "## 2026-01-01 — Раздел\n- исправленный факт\n")
        change_sha = self.commit("correct and remove")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        delta = next(name for name in self.remote_files() if change_sha[:12] in name)
        body = self.remote_tree(delta).stdout
        self.assertIn("− - старый факт", body)
        self.assertIn("− - удаляемый факт", body)
        self.assertIn("+ - исправленный факт", body)
        self.assertIn("удалено: ## 2026-01-02 — Удалённый", body)

    def test_empty_change_advances_checkpoint_and_dry_run_is_read_only(self):
        self.write("devlog.md", "# Только памятка обновлена\n")
        self.commit("header only")
        refs_before = git(self.repo, "show-ref").stdout
        objects_before = git(self.repo, "count-objects", "-v").stdout
        fetch_head = self.repo / ".git" / "FETCH_HEAD"
        fetch_before = fetch_head.read_bytes() if fetch_head.exists() else None
        dry = self.publish("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(git(self.repo, "show-ref").stdout, refs_before)
        self.assertEqual(git(self.repo, "count-objects", "-v").stdout, objects_before)
        self.assertEqual(fetch_head.read_bytes() if fetch_head.exists() else None, fetch_before)
        self.assertEqual(
            git(
                self.repo, "--git-dir", str(self.remote), "for-each-ref", "refs/heads/updates"
            ).stdout,
            "",
        )
        actual = self.publish()
        self.assertEqual(actual.returncode, 0, actual.stderr)
        state = self.remote_tree(".devlog-state.json").stdout
        self.assertEqual(
            json.loads(state)["source_commit"], git(self.repo, "rev-parse", "HEAD").stdout.strip()
        )
        self.assertFalse(any(name.startswith("updates/") for name in self.remote_files()))
        tip = git(self.repo, "--git-dir", str(self.remote), "rev-parse", "updates").stdout
        repeated = self.publish()
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(
            git(self.repo, "--git-dir", str(self.remote), "rev-parse", "updates").stdout, tip
        )

    def test_runs_from_linked_worktree(self):
        self.write("devlog.md", "## 2026-01-01 — Worktree\n- начальная запись\n")
        self.commit("first devlog source commit")
        self.write("devlog.md", "## 2026-01-01 — Worktree\n- начальная запись\n- работает\n")
        self.commit("worktree change")
        linked = self.base / "linked"
        git(self.repo, "worktree", "add", str(linked), "HEAD")
        result = self.publish(cwd=linked)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(name.startswith("updates/") for name in self.remote_files()))
        self.assertEqual(git(linked, "status", "--porcelain").stdout, "")

    def test_existing_updates_branch_bootstraps_after_historical_cleanup(self):
        project = SCRIPT.parents[1]
        clone = self.base / "bootstrap-source"
        git(self.base, "clone", "--no-hardlinks", str(project), str(clone))
        git(clone, "checkout", "--detach", "bbe577bb98017e31316d9f013c5c488b30909e73")
        bootstrap_remote = self.base / "bootstrap-origin.git"
        git(self.base, "init", "--bare", str(bootstrap_remote))
        git(clone, "config", "user.name", "Test")
        git(clone, "config", "user.email", "test@example.com")
        seed = self.base / "updates-seed"
        seed.mkdir()
        git(seed, "init", "-b", "main")
        git(seed, "config", "user.name", "Test")
        git(seed, "config", "user.email", "test@example.com")
        (seed / "updates").mkdir()
        (seed / "updates" / "old-test-record.md").write_text("historical test output\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "existing updates content")
        git(seed, "remote", "add", "origin", str(bootstrap_remote))
        git(seed, "push", "origin", "HEAD:refs/heads/updates")
        git(clone, "remote", "set-url", "origin", str(bootstrap_remote))
        git(clone, "push", "origin", "HEAD:refs/heads/main")

        before = git(clone, "status", "--porcelain").stdout
        result = self.publish(cwd=clone)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(clone, "status", "--porcelain").stdout, before)
        files = git(
            clone, "--git-dir", str(bootstrap_remote), "ls-tree", "-r", "--name-only", "updates"
        ).stdout.splitlines()
        self.assertIn("updates/old-test-record.md", files)
        self.assertIn(".devlog-state.json", files)
        self.assertFalse(any(name.startswith("updates/20") for name in files))
        state = git(
            clone, "--git-dir", str(bootstrap_remote), "show", "updates:.devlog-state.json"
        ).stdout
        self.assertEqual(
            json.loads(state)["source_commit"], git(clone, "rev-parse", "HEAD").stdout.strip()
        )

    def test_divergent_checkpoint_fails_without_publishing(self):
        self.write("devlog.md", "## 2026-01-01 — Запись\n- исходная\n")
        self.commit("first source baseline")
        git(self.repo, "checkout", "--orphan", "divergent")
        (self.repo / "unrelated.txt").write_text("unrelated history\n")
        self.commit("unrelated source history")
        divergent = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "checkout", "main")

        seed = self.base / "state-seed"
        seed.mkdir()
        git(seed, "init", "-b", "main")
        git(seed, "config", "user.name", "Test")
        git(seed, "config", "user.email", "test@example.com")
        (seed / ".devlog-state.json").write_text(
            json.dumps({"version": 1, "source_commit": divergent}) + "\n", encoding="utf-8"
        )
        git(seed, "add", ".devlog-state.json")
        git(seed, "commit", "-m", "unrelated checkpoint")
        git(seed, "remote", "add", "origin", str(self.remote))
        git(seed, "push", "origin", "HEAD:refs/heads/updates")

        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("переписана или расходится", result.stderr)
        files = self.remote_files()
        self.assertIn(".devlog-state.json", files)
        self.assertFalse(any(name.startswith("updates/") for name in files))


if __name__ == "__main__":
    unittest.main()
