#!/usr/bin/env python3
"""Publish chronological, append-only devlog deltas to the ``updates`` branch."""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# Keep CLI runs, including --dry-run, from creating untracked bytecode files.
sys.dont_write_bytecode = True
from devlog_schema import render_record, validate_record

DEVLOG = "devlog.md"
ENTRIES = "entries"
UPDATES_DIR = "updates"
STATE_FILE = ".devlog-state.json"
UPDATES_BRANCH = "updates"
BOT_NAME = "devlog-bot"
BOT_EMAIL = "devlog-bot@users.noreply.github.com"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# The existing updates branch contains test output through this cleanup commit.
EXISTING_BRANCH_BASELINE = "bbe577bb98017e31316d9f013c5c488b30909e73"
RETRIES = 5


class PublishError(Exception):
    pass


def _safe_error(value):
    value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[credentials]@", value)
    value = re.sub(r"(https?://)[^\s]+", r"\1[remote]", value)
    value = re.sub(r"[A-Za-z0-9_=-]{32,}", "[redacted]", value)
    return value.strip()


def run(args, cwd=None, env=None, input_text=None, check=True):
    result = subprocess.run(
        args, cwd=cwd, env=env, input=input_text, capture_output=True, text=True
    )
    if check and result.returncode:
        operation = args[1] if len(args) > 1 else "command"
        detail = _safe_error(result.stderr or result.stdout)
        raise PublishError(
            "git %s завершился с кодом %d%s"
            % (operation, result.returncode, (": " + detail[:500]) if detail else "")
        )
    return result


def git(args, **kwargs):
    return run(["git"] + list(args), **kwargs).stdout.strip()


def parse_sections(text):
    order, sections, current, body = [], {}, None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = body
            current = line.rstrip()
            order.append(current)
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        sections[current] = body
    return order, sections


def trim_tail(lines):
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def section_delta(old_text, new_text):
    old_order, old_sections = parse_sections(old_text)
    new_order, new_sections = parse_sections(new_text)
    chunks = []
    for heading in new_order:
        new = trim_tail(list(new_sections[heading]))
        if heading not in old_sections:
            if new:
                chunks.append([heading] + new)
            continue
        old = trim_tail(list(old_sections[heading]))
        matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        changes = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "delete"):
                changes.extend("− " + line for line in old[i1:i2] if line.strip())
            if tag in ("replace", "insert"):
                changes.extend("+ " + line for line in new[j1:j2] if line.strip())
        if changes:
            chunks.append([heading] + changes)
    for heading in old_order:
        if heading not in new_sections:
            chunks.append(["удалено: " + heading])
    return chunks


def commit_time(root, sha):
    return datetime.fromtimestamp(
        int(git(["show", "-s", "--format=%ct", sha], cwd=root)), timezone.utc
    )


def source_commits(root):
    result = []
    candidates = git(
        [
            "log",
            "--first-parent",
            "--full-history",
            "--reverse",
            "--format=%H",
            "HEAD",
            "--",
            DEVLOG,
            ENTRIES,
        ],
        cwd=root,
    ).splitlines()
    for sha in candidates:
        paths = git(
            ["diff-tree", "--root", "--first-parent", "--no-commit-id", "--name-only", "-r", sha],
            cwd=root,
        ).splitlines()
        if any(
            path == DEVLOG or path.startswith(ENTRIES + "/") and path.endswith(".json")
            for path in paths
        ):
            result.append(sha)
    return result


def first_devlog_commit(root, commits):
    for sha in commits:
        paths = git(
            ["diff-tree", "--root", "--first-parent", "--no-commit-id", "--name-only", "-r", sha],
            cwd=root,
        ).splitlines()
        if DEVLOG in paths:
            return sha
    return None


def entry_records(root, sha):
    changed = git(
        ["diff-tree", "--root", "--first-parent", "--no-commit-id", "--name-only", "-r", sha],
        cwd=root,
    ).splitlines()
    added = set(
        git(
            [
                "diff-tree",
                "--root",
                "--first-parent",
                "--no-commit-id",
                "--diff-filter=A",
                "--name-only",
                "-r",
                sha,
            ],
            cwd=root,
        ).splitlines()
    )
    records = []
    for path in changed:
        if not (path.startswith(ENTRIES + "/") and path.endswith(".json")):
            continue
        if not re.fullmatch(r"entries/\d{4}-\d{2}-\d{2}/[^/]+\.json", path):
            raise PublishError("Некорректный путь immutable записи: %s" % path)
        if path not in added:
            raise PublishError(
                "Записи entries неизменяемы; изменение или удаление %s в %s" % (path, sha[:12])
            )
        raw = git(["show", sha + ":" + path], cwd=root)
        try:
            item = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise PublishError("Некорректный JSON %s в %s: %s" % (path, sha[:12], exc))
        try:
            item = validate_record(item)
        except ValueError as exc:
            raise PublishError("Некорректная запись %s: %s" % (path, exc))
        if path.rsplit("/", 1)[1][:-5] != item["id"]:
            raise PublishError("Имя файла записи должно совпадать с id: %s" % path)
        created_day = (
            datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%d")
        )
        if path.split("/")[1] != created_day:
            raise PublishError("Дата каталога записи должна совпадать с created_at: %s" % path)
        records.append((path, item))
    return records


def delta_for_commit(root, sha, include_devlog=True):
    chunks = []
    paths = git(
        ["diff-tree", "--root", "--first-parent", "--no-commit-id", "--name-only", "-r", sha],
        cwd=root,
    ).splitlines()
    if include_devlog and DEVLOG in paths:
        parent = run(["git", "rev-parse", sha + "^"], cwd=root, check=False)
        before = ""
        if parent.returncode == 0:
            before = git(["show", parent.stdout.strip() + ":" + DEVLOG], cwd=root, check=False)
        after_result = git(["show", sha + ":" + DEVLOG], cwd=root, check=False)
        after = after_result if after_result else ""
        chunks.extend(section_delta(before, after))
    for path, item in entry_records(root, sha):
        chunks.append(render_record(item).splitlines())
    return chunks


def read_state(root, parent):
    if not parent:
        return None
    exists = run(["git", "cat-file", "-e", parent + ":" + STATE_FILE], cwd=root, check=False)
    if exists.returncode:
        return None
    result = git(["show", parent + ":" + STATE_FILE], cwd=root, check=False)
    try:
        data = json.loads(result)
        if not isinstance(data, dict):
            raise ValueError("state должен быть JSON объектом")
        if data.get("version") != 1:
            raise ValueError("unsupported state version")
        sha = data["source_commit"]
        if not isinstance(sha, str):
            raise ValueError("source_commit должен быть строкой")
        check = run(["git", "cat-file", "-e", sha + "^{commit}"], cwd=root, check=False)
        if check.returncode:
            raise ValueError("source commit is not present")
        return sha
    except (ValueError, KeyError, TypeError) as exc:
        raise PublishError("Некорректный %s в ветке updates: %s" % (STATE_FILE, exc))


def commit_is_ancestor(root, ancestor, descendant):
    return (
        run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=root, check=False
        ).returncode
        == 0
    )


def pending_commits(
    root, checkpoint, commits, source_head, baseline_entries=None, bootstrap_baseline=None
):
    if bootstrap_baseline:
        return [(sha, sha != bootstrap_baseline) for sha in commits]
    if checkpoint:
        if not commit_is_ancestor(root, checkpoint, source_head):
            raise PublishError(
                "История main переписана или расходится с checkpoint %s; публикация остановлена"
                % checkpoint[:12]
            )
        try:
            start = commits.index(checkpoint) + 1
        except ValueError:
            # A valid checkpoint may not itself alter a tracked source file.
            newer = [sha for sha in commits if commit_is_ancestor(root, checkpoint, sha)]
            start = commits.index(newer[0]) if newer else len(commits)
        pending = commits[start:]
        if baseline_entries in commits and baseline_entries not in pending:
            pending.insert(0, baseline_entries)
        return [(sha, sha != baseline_entries) for sha in pending]
    return [(sha, True) for sha in commits]


def build_updates_commit(
    root, parent, checkpoint, commits, dry_run=False, baseline_entries=None, bootstrap_baseline=None
):
    updates = []
    for sha, include_devlog in pending_commits(
        root,
        checkpoint,
        commits,
        git(["rev-parse", "HEAD"], cwd=root),
        baseline_entries,
        bootstrap_baseline,
    ):
        chunks = delta_for_commit(root, sha, include_devlog)
        if chunks:
            day = commit_time(root, sha).strftime("%Y-%m-%d")
            short = sha[:12]
            path = "%s/%s-%s.md" % (UPDATES_DIR, day, short)
            lines = ["# Обновление девлога — " + day + " (коммит " + short + ")", ""]
            for chunk in chunks:
                lines.extend(chunk)
                lines.append("")
            updates.append((path, "\n".join(lines).rstrip() + "\n"))
    final_state = {"version": 1, "source_commit": git(["rev-parse", "HEAD"], cwd=root)}
    print("Новых записей: %d; checkpoint: %s" % (len(updates), final_state["source_commit"][:12]))
    for path, content in updates:
        print("  " + path)
        if dry_run:
            print(content, end="")
    return updates, final_state


def publish(root, dry_run=False):
    source_head = git(["rev-parse", "HEAD"], cwd=root)
    commits = source_commits(root)
    if not commits:
        print("Нет коммитов devlog.md или entries; публикация не требуется")
        return 0
    for attempt in range(RETRIES):
        remote = git(["ls-remote", "--heads", "origin", "refs/heads/" + UPDATES_BRANCH], cwd=root)
        parent = None
        if remote:
            if dry_run:
                cached = run(
                    ["git", "rev-parse", "--verify", "refs/remotes/origin/" + UPDATES_BRANCH],
                    cwd=root,
                    check=False,
                )
                if cached.returncode == 0:
                    parent = cached.stdout.strip()
                    if parent != remote.split()[0]:
                        print(
                            "Предупреждение: dry-run использует устаревшую локальную ветку updates"
                        )
                else:
                    print(
                        "Предупреждение: dry-run не загружает удалённый checkpoint; preview может включать уже опубликованное"
                    )
            else:
                git(["fetch", "--quiet", "origin", "refs/heads/" + UPDATES_BRANCH], cwd=root)
                parent = git(["rev-parse", "FETCH_HEAD"], cwd=root)
        stored_checkpoint = read_state(root, parent)
        checkpoint = stored_checkpoint
        if checkpoint is None and parent:
            checkpoint = EXISTING_BRANCH_BASELINE
        if checkpoint and not commit_is_ancestor(root, checkpoint, source_head):
            raise PublishError(
                "История main переписана или расходится с checkpoint %s; публикация остановлена"
                % checkpoint[:12]
            )
        bootstrap_baseline = first_devlog_commit(root, commits) if not parent else None
        baseline_entries = None
        if parent and stored_checkpoint is None:
            baseline_entries = checkpoint
        updates, state = build_updates_commit(
            root, parent, checkpoint, commits, dry_run, baseline_entries, bootstrap_baseline
        )
        if dry_run:
            return 0
        index_path = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="devlog-updates-",
                dir=git(["rev-parse", "--absolute-git-dir"], cwd=root),
                delete=False,
            ) as handle:
                index_path = handle.name
            env = dict(os.environ, GIT_INDEX_FILE=index_path)
            git(["read-tree", parent or EMPTY_TREE], cwd=root, env=env)
            for path, content in updates:
                blob = run(
                    ["git", "hash-object", "-w", "--stdin"], cwd=root, input_text=content
                ).stdout.strip()
                git(
                    ["update-index", "--add", "--cacheinfo", "100644," + blob + "," + path],
                    cwd=root,
                    env=env,
                )
            state_blob = run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=root,
                input_text=json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
            ).stdout.strip()
            git(
                ["update-index", "--add", "--cacheinfo", "100644," + state_blob + "," + STATE_FILE],
                cwd=root,
                env=env,
            )
            tree = git(["write-tree"], cwd=root, env=env)
            # Avoid a noisy checkpoint commit when this exact source tip is already recorded.
            existing = (
                git(["show", parent + ":" + STATE_FILE], cwd=root, check=False) if parent else ""
            )
            if not updates and existing == json.dumps(state, ensure_ascii=False, sort_keys=True):
                print("Нет новых изменений; checkpoint уже актуален")
                return 0
            ident = dict(os.environ)
            for key, value in (
                ("GIT_AUTHOR_NAME", BOT_NAME),
                ("GIT_AUTHOR_EMAIL", BOT_EMAIL),
                ("GIT_COMMITTER_NAME", BOT_NAME),
                ("GIT_COMMITTER_EMAIL", BOT_EMAIL),
            ):
                ident.setdefault(key, value)
            args = ["git", "commit-tree", tree, "-m", "devlog: обновления до " + source_head[:12]]
            if parent:
                args += ["-p", parent]
            new_commit = run(args, cwd=root, env=ident).stdout.strip()
            pushed = run(
                ["git", "push", "origin", new_commit + ":refs/heads/" + UPDATES_BRANCH],
                cwd=root,
                check=False,
            )
            if pushed.returncode:
                # Retry only if branch moved; other failures are reported safely.
                latest = git(
                    ["ls-remote", "--heads", "origin", "refs/heads/" + UPDATES_BRANCH], cwd=root
                )
                if attempt + 1 < RETRIES and latest and latest.split()[0] != (parent or ""):
                    continue
                raise PublishError(
                    "git push завершился с кодом %d (удалённый сервер отклонил push или возникла транспортная ошибка)"
                    % pushed.returncode
                )
            print(
                "Опубликовано в ветку updates: %d файлов, checkpoint %s"
                % (len(updates), source_head[:12])
            )
            return 0
        finally:
            if index_path and os.path.exists(index_path):
                os.unlink(index_path)
    raise PublishError(
        "Ветка updates часто обновлялась параллельно; исчерпаны %d попыток" % RETRIES
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="показать дельту без записи и push")
    args = parser.parse_args(argv)
    try:
        root = git(["rev-parse", "--show-toplevel"])
        return publish(root, args.dry_run)
    except PublishError as exc:
        print("Ошибка публикации: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
