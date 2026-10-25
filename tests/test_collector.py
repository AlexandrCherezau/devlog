from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import devlog_schema as schema
import install_codex
import record_update as collector


def record():
    return {
        "version": 1,
        "id": "fixture-task-001",
        "created_at": "2026-10-25T13:00:00+00:00",
        "project": "guild",
        "title": "Кланы читают общий уровень",
        "changes": ["Используется общий уровень персонажа"],
        "status": "tested",
        "verification": ["Проверено соответствие API"],
        "notes": [],
    }


class SchemaTests(unittest.TestCase):
    def test_record_renders_actual_status_and_limits(self):
        value = record()
        value["notes"] = ["Клиентская проверка ещё не выполнена"]
        rendered = schema.render_record(value)
        self.assertIn("Проверено локально, ещё не выложено", rendered)
        self.assertIn(value["notes"][0], rendered)
        self.assertNotIn("Установлено на основном сервере", rendered)

    def test_credential_and_private_path_are_rejected(self):
        for text in [
            "token=private-value",
            "/home/person/private/source",
            "ghp_" + "A" * 32,
            "123456789:" + "A" * 35,
            "https://user:password@example.invalid/path",
            "two\nlines",
            "/srv/eldheim/config/private.yml",
            "Bearer eyJhbGci.test.signature",
            "/etc/private.yml",
            "AWS_SECRET_ACCESS_KEY=fake-value-for-filter-test",
            '"DATABASE_PASSWORD": "fake-test-value"',
        ]:
            with self.subTest(text=text[:15]), self.assertRaises(ValueError):
                value = record()
                value["changes"] = [text]
                schema.validate_record(value)

    def test_malformed_or_unknown_records_rejected(self):
        for field, value in [
            ("version", True),
            ("project", "../../private"),
            ("status", []),
            ("created_at", "2026-10-25"),
            ("changes", []),
            ("source_revision", 17),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                changed = record()
                changed[field] = value
                schema.validate_record(changed)
        value = record()
        value["raw_transcript"] = "private conversation"
        with self.assertRaises(ValueError):
            schema.validate_record(value)

    def test_fingerprint_ignores_only_transport_identity_time(self):
        first = record()
        second = deepcopy(first)
        second["id"] = "other-task-002"
        second["created_at"] = "2026-10-26T16:00:00+00:00"
        self.assertEqual(schema.fingerprint(first), schema.fingerprint(second))
        second["status"] = "deployed-prod"
        self.assertNotEqual(schema.fingerprint(first), schema.fingerprint(second))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "repository": "Example/devlog",
            "project_root": self.root,
            "state_dir": self.root / "state",
            "publish": True,
        }
        self.remote = {}
        self.puts = 0

    def api(self, method, endpoint, payload=None):
        key = endpoint.split("?")[0]
        if method == "GET":
            if key not in self.remote:
                raise collector.GitHubError(404)
            return {"content": base64.b64encode(self.remote[key]).decode()}
        self.puts += 1
        self.remote[key] = base64.b64decode(payload["content"])
        return {"commit": {"sha": "a" * 40}}

    def test_retry_queue_to_receipt_and_repeated_add_no_new_push(self):
        value, queued = collector.add_record(self.config, record())
        self.assertTrue(queued)
        self.assertEqual(collector.flush(self.config, api=self.api), (1, 0))
        self.assertEqual(len(list((self.config["state_dir"] / "pending").glob("*.json"))), 0)
        later = record()
        later["created_at"] = "2026-10-26T14:00:00+00:00"
        _, queued = collector.add_record(self.config, later)
        self.assertFalse(queued)
        self.assertEqual(self.puts, 1)

    def test_duplicate_queue_preserves_first_timestamp(self):
        collector.add_record(self.config, record())
        later = record()
        later["created_at"] = "2026-10-26T14:00:00+00:00"
        stored, _ = collector.add_record(self.config, later)
        self.assertEqual(stored["created_at"], record()["created_at"])

    def test_distinct_explicit_ids_do_not_duplicate_identical_facts(self):
        collector.add_record(self.config, record())
        second = record()
        second["id"] = "another-task-002"
        stored, queued = collector.add_record(self.config, second)
        self.assertTrue(queued)
        self.assertEqual(stored["id"], record()["id"])
        self.assertEqual(collector.flush(self.config, api=self.api), (1, 0))
        _, queued = collector.add_record(self.config, second)
        self.assertFalse(queued)
        self.assertEqual(self.puts, 1)

    def test_network_failure_retains_complete_record(self):
        collector.add_record(self.config, record())

        def offline(*args):
            raise collector.GitHubError(None)

        self.assertEqual(collector.flush(self.config, api=offline), (0, 1))
        saved = json.loads((self.config["state_dir"] / "pending/fixture-task-001.json").read_text())
        self.assertEqual(saved, record())
        self.assertEqual(collector.flush(self.config, api=self.api), (1, 0))

    def test_unknown_put_result_is_reconciled_next_attempt(self):
        collector.add_record(self.config, record())

        def ambiguous(method, endpoint, payload=None):
            result = self.api(method, endpoint, payload)
            if method == "PUT":
                raise collector.GitHubError(None)
            return result

        self.assertEqual(collector.flush(self.config, api=ambiguous), (0, 1))
        self.assertEqual(collector.flush(self.config, api=self.api), (1, 0))
        self.assertEqual(self.puts, 1)

    def test_conflicting_agent_create_is_retried(self):
        attempts = 0

        def racing(method, endpoint, payload=None):
            nonlocal attempts
            if method == "PUT":
                attempts += 1
                if attempts < 3:
                    raise collector.GitHubError(409)
            return self.api(method, endpoint, payload)

        with patch.object(collector.time, "sleep"):
            collector.publish_record(self.config, record(), racing)
        self.assertEqual(attempts, 3)

    def test_existing_different_remote_record_is_never_overwritten(self):
        collector.publish_record(self.config, record(), self.api)
        changed = record()
        changed["title"] = "Другая задача"
        with self.assertRaises(ValueError):
            collector.publish_record(self.config, changed, self.api)
        self.assertEqual(self.puts, 1)

    def test_other_agent_timestamp_does_not_overwrite_or_block_same_facts(self):
        collector.publish_record(self.config, record(), self.api)
        later = record()
        later["created_at"] = "2026-10-25T14:00:00+00:00"
        collector.publish_record(self.config, later, self.api)
        self.assertEqual(self.puts, 1)
        stored = json.loads(next(iter(self.remote.values())))
        self.assertEqual(stored["created_at"], record()["created_at"])

    def test_same_id_with_different_queued_facts_rejected(self):
        collector.add_record(self.config, record())
        changed = record()
        changed["changes"] = ["Другое изменение"]
        with self.assertRaises(ValueError):
            collector.add_record(self.config, changed)

    def test_crashed_writer_does_not_expose_temp_file_to_flush(self):
        pending = self.config["state_dir"] / "pending"
        pending.mkdir(parents=True)
        (pending / ".partial.tmp").write_text("{")
        self.assertEqual(collector.flush(self.config, api=self.api), (0, 0))


class InstallerTests(unittest.TestCase):
    def test_scoped_install_preserves_instructions_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            user_home = Path(temporary) / "user"
            root = user_home / "projects/server"
            root.mkdir(parents=True)
            codex = user_home / ".codex"
            codex.mkdir()
            instructions = codex / "AGENTS.md"
            instructions.write_text("Existing user rules.\n")
            config = user_home / ".config/eldheim-devlog/config.json"
            with patch.dict("os.environ", {"XDG_STATE_HOME": str(user_home / ".local/state")}):
                for _ in range(2):
                    install_codex.install(
                        "Example/devlog",
                        root,
                        timer=False,
                        user_home=user_home,
                        config_path=config,
                        codex_home=codex,
                    )
            text = instructions.read_text()
            self.assertTrue(text.startswith("Existing user rules."))
            self.assertEqual(text.count(install_codex.BEGIN), 1)
            self.assertIn(str(root), text)
            self.assertTrue((codex / "skills/eldheim-devlog/scripts/record_update.py").is_file())
            self.assertTrue((root / ".agents/skills/eldheim-devlog/SKILL.md").is_file())
            self.assertEqual(collector.load_config(config)["repository"], "Example/devlog")

    def test_entire_home_scope_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                install_codex.install(
                    "Example/devlog",
                    root,
                    timer=False,
                    user_home=root,
                    config_path=root / "config.json",
                    codex_home=root / ".codex",
                )

    def test_symlink_and_unsupported_timer_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            user_home = Path(temporary) / "user"
            root = user_home / "project"
            root.mkdir(parents=True)
            external = Path(temporary) / "external"
            external.mkdir()
            (external / "SKILL.md").write_text("must remain untouched")
            destination = user_home / ".agents/skills/eldheim-devlog"
            destination.parent.mkdir(parents=True)
            destination.symlink_to(external, target_is_directory=True)
            with self.assertRaises(ValueError):
                install_codex.install(
                    "Example/devlog",
                    root,
                    timer=False,
                    user_home=user_home,
                    config_path=user_home / "config.json",
                    codex_home=user_home / ".codex",
                )
            self.assertEqual((external / "SKILL.md").read_text(), "must remain untouched")
            destination.unlink()
            with (
                patch.object(install_codex.sys, "platform", "darwin"),
                self.assertRaises(ValueError),
            ):
                install_codex.install(
                    "Example/devlog",
                    root,
                    timer=True,
                    user_home=user_home,
                    config_path=user_home / "config.json",
                    codex_home=user_home / ".codex",
                )
            self.assertFalse(destination.exists())
            self.assertFalse((user_home / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
