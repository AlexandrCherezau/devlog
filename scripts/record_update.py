#!/usr/bin/env python3
"""Queue curated facts and publish immutable GitHub records using existing gh auth."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from types import ModuleType

from devlog_schema import STATUSES, canonical_json, fingerprint, validate_record

fcntl: ModuleType | None
try:
    fcntl = import_module("fcntl")
except ImportError:
    fcntl = None


def default_config() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "eldheim-devlog/config.json"
    )


def load_config(path: Path) -> dict:
    config = json.loads(path.expanduser().read_text())
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config.get("repository", "")):
        raise ValueError("Configure repository as OWNER/REPOSITORY")
    if config.get("publish") is not True:
        raise ValueError("Publication is not enabled in this configuration")
    root = Path(config["project_root"]).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Configured project_root does not exist")
    config["project_root"] = root
    config["state_dir"] = Path(config["state_dir"]).expanduser()
    return config


@contextmanager
def queue_transaction(state: Path):
    if fcntl is None:
        raise ValueError("Use Linux, macOS or WSL; native Windows file locking is not supported")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "add.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def add_record(config: dict, record: dict) -> tuple[dict, bool]:
    validate_record(record)
    with queue_transaction(config["state_dir"]):
        return _add_record(config, record)


def _add_record(config: dict, record: dict) -> tuple[dict, bool]:
    state = config["state_dir"]
    receipt = state / "published" / (record["id"] + ".json")
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        if previous["fingerprint"] != fingerprint(record):
            raise ValueError("This id was already published with different facts")
        return record, False
    pending_same_id = state / "pending" / (record["id"] + ".json")
    if pending_same_id.exists():
        previous = validate_record(json.loads(pending_same_id.read_text()))
        if fingerprint(previous) != fingerprint(record):
            raise ValueError("This id is already queued with different facts")
        return previous, True
    digest = fingerprint(record)
    for path in (state / "published").glob("*.json"):
        previous = json.loads(path.read_text())
        if previous["fingerprint"] == digest:
            return dict(record, id=previous["id"]), False
    for path in (state / "pending").glob("*.json"):
        previous = validate_record(json.loads(path.read_text()))
        if fingerprint(previous) == digest:
            return previous, True
    target = state / "pending" / (record["id"] + ".json")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        # Link a complete durable file, never expose half-written JSON to another agent.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix="." + record["id"] + "-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(canonical_json(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError:
        previous = validate_record(json.loads(target.read_text()))
        if fingerprint(previous) != fingerprint(record):
            raise ValueError("This id is already queued with different facts")
        return previous, True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return record, True


class GitHubError(RuntimeError):
    def __init__(self, status: int | None):
        self.status = status
        super().__init__(
            f"GitHub request failed (HTTP {status or 'network/auth error'}); record retained in queue"
        )


def github(method: str, endpoint: str, payload: dict | None = None) -> dict:
    args = ["gh", "api", "--method", method, endpoint]
    if payload is not None:
        args += ["--input", "-"]
    result = subprocess.run(
        args,
        input=None if payload is None else json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        match = re.search(r"HTTP (\d{3})", result.stderr)
        # Never echo headers, auth environment, or arbitrary API error bodies.
        raise GitHubError(int(match.group(1)) if match else None)
    return json.loads(result.stdout)


def publish_record(config: dict, record: dict, api=github) -> str:
    validate_record(record)
    repository = config["repository"]
    created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
    path = f"entries/{created.astimezone(timezone.utc):%Y-%m-%d}/{record['id']}.json"
    endpoint = f"repos/{repository}/contents/{path}"
    content = canonical_json(record).encode()
    for attempt in range(4):
        try:
            existing = api("GET", endpoint + "?ref=main")
        except GitHubError as failure:
            if failure.status != 404:
                raise
        else:
            stored = validate_record(json.loads(base64.b64decode(existing.get("content", ""))))
            if stored["id"] != record["id"] or fingerprint(stored) != fingerprint(record):
                raise ValueError(
                    "Remote id collision: refusing to replace an existing public record"
                )
            return path
        try:
            api(
                "PUT",
                endpoint,
                {
                    "message": f"devlog({record['project']}): {record['title']}",
                    "content": base64.b64encode(content).decode(),
                    "branch": "main",
                },
            )
            return path
        except GitHubError as failure:
            if failure.status not in {409, 422} or attempt == 3:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError("Publication retry limit reached")


def flush(config: dict, *, api=github, limit: int = 50) -> tuple[int, int]:
    if fcntl is None:
        raise ValueError("Use Linux, macOS or WSL; native Windows file locking is not supported")
    state = config["state_dir"]
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "flush.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another publisher is active; queued records are retained.")
            return 0, 0
        published, failed = 0, 0
        for pending in sorted((state / "pending").glob("*.json"))[:limit]:
            try:
                record = validate_record(json.loads(pending.read_text()))
                path = publish_record(config, record, api)
                with queue_transaction(state):
                    atomic_json(
                        state / "published" / pending.name,
                        {
                            "id": record["id"],
                            "fingerprint": fingerprint(record),
                            "repository": config["repository"],
                            "path": path,
                            "published_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    pending.unlink()
                published += 1
                print(
                    f"Published {record['id']}: https://github.com/{config['repository']}/blob/main/{path}"
                )
            except (ValueError, OSError, GitHubError, subprocess.SubprocessError) as failure:
                failed += 1
                print(f"Queued {pending.stem}: {failure}", file=sys.stderr)
        return published, failed


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--config", type=Path, default=default_config())
    sub = command.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="Record one completed task; by default also flush its queue")
    add.add_argument("--project", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--change", action="append", required=True)
    add.add_argument("--status", choices=STATUSES, required=True)
    add.add_argument("--verification", action="append", default=[])
    add.add_argument("--note", action="append", default=[])
    add.add_argument("--source-revision")
    add.add_argument("--id")
    add.add_argument("--queue-only", action="store_true")
    add.add_argument("--dry-run", action="store_true")
    sub.add_parser("flush", help="Retry retained records; no transcript/source scanning")
    sub.add_parser("status", help="Show queue and receipt counts")
    return command


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "status":
            state = config["state_dir"]
            print(
                json.dumps(
                    {
                        "repository": config["repository"],
                        "pending": len(list((state / "pending").glob("*.json"))),
                        "published": len(list((state / "published").glob("*.json"))),
                    },
                    indent=2,
                )
            )
        elif args.command == "flush":
            _, failed = flush(config)
            return int(bool(failed))
        else:
            record = {
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "project": args.project,
                "title": args.title,
                "changes": args.change,
                "status": args.status,
                "verification": args.verification,
                "notes": args.note,
            }
            if args.source_revision:
                record["source_revision"] = args.source_revision
            record["id"] = args.id or fingerprint(record)[:24]
            validate_record(record)
            if args.dry_run:
                print(canonical_json(record), end="")
                return 0
            try:
                Path.cwd().resolve().relative_to(config["project_root"])
            except ValueError as failure:
                raise ValueError(
                    "Record from within the configured project root; unrelated projects are excluded"
                ) from failure
            record, queued = add_record(config, record)
            print(("Queued " if queued else "Already published ") + record["id"])
            if queued and not args.queue_only:
                _, failed = flush(config)
                if failed:
                    print(
                        "Saved locally. The installed timer will retry publication.",
                        file=sys.stderr,
                    )
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as failure:
        print(f"Devlog: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
