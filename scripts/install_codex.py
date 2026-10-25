#!/usr/bin/env python3
"""Install the scoped Codex skill, durable queue and optional Linux retry timer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from record_update import atomic_json, default_config

BEGIN = "<!-- ELDHEIM-DEVLOG:START -->"
END = "<!-- ELDHEIM-DEVLOG:END -->"


def managed_block(path: Path, body: str) -> None:
    original = path.read_text() if path.exists() else ""
    block = BEGIN + "\n" + body.strip() + "\n" + END
    if BEGIN in original or END in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise ValueError(f"Malformed managed block in {path}")
        updated = re.sub(
            re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block, original, flags=re.S
        )
    else:
        updated = original.rstrip() + ("\n\n" if original.strip() else "") + block + "\n"
    if updated != original:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(
                path.name + ".before-devlog-" + hashlib.sha256(original.encode()).hexdigest()[:10]
            )
            if not backup.exists():
                shutil.copy2(path, backup)
        path.write_text(updated)


def systemd_argument(value: str) -> str:
    return json.dumps(value.replace("%", "%%"), ensure_ascii=False)


def install(
    repository: str,
    root: Path,
    *,
    timer: bool,
    user_home: Path,
    config_path: Path,
    codex_home: Path,
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Use OWNER/REPOSITORY")
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Project root must be a directory")
    if root == user_home.resolve() or root == Path("/"):
        raise ValueError("Choose the Eldheim/project directory, not the entire home or filesystem")
    if sys.platform == "win32":
        raise ValueError(
            "Use WSL and run Codex there; native Windows installation is not supported"
        )
    if timer and (sys.platform != "linux" or not shutil.which("systemctl")):
        raise ValueError(
            "systemd timer requires Linux; install with --no-timer on macOS/other Unix"
        )
    source = Path(__file__).resolve().parents[1]
    skill = user_home / ".agents/skills/eldheim-devlog"
    # Validate destinations before copying a file or changing any instructions.
    for target in (skill, config_path, root / "AGENTS.md", codex_home / "AGENTS.md"):
        if target.is_symlink():
            raise ValueError(f"Refusing to overwrite a symlink destination: {target}")
    if skill.exists() and any(path.is_symlink() for path in skill.rglob("*")):
        raise ValueError("Existing skill contains symlinks; refusing to overwrite external targets")
    for alias in (codex_home / "skills/eldheim-devlog", root / ".agents/skills/eldheim-devlog"):
        if alias.is_symlink() and alias.resolve() != skill.resolve():
            raise ValueError(f"Existing skill alias points elsewhere: {alias}")
        if alias.exists() and not alias.is_symlink() and alias.resolve() != skill.resolve():
            raise ValueError(f"Existing skill directory needs manual migration: {alias}")
    if config_path.exists():
        old = json.loads(config_path.read_text())
        if old.get("repository") != repository or old.get("project_root") != str(root):
            raise ValueError(
                "Existing installation targets a different repository/root; use a separate --config"
            )
    skill.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / "skills/eldheim-devlog", skill, dirs_exist_ok=True)
    helpers = skill / "scripts"
    helpers.mkdir(exist_ok=True)
    for name in ("record_update.py", "devlog_schema.py"):
        shutil.copyfile(source / "scripts" / name, helpers / name)
    alias = codex_home / "skills/eldheim-devlog"
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.is_symlink():
        if alias.resolve() != skill.resolve():
            raise ValueError("Existing Codex skill symlink points elsewhere")
    elif alias.exists():
        if alias.resolve() != skill.resolve():
            raise ValueError("An existing Codex skill directory needs manual migration")
    else:
        alias.symlink_to(skill, target_is_directory=True)
    state_base = Path(os.environ.get("XDG_STATE_HOME", str(user_home / ".local/state")))
    state = state_base / "eldheim-devlog" / repository.replace("/", "--")
    config = {
        "version": 1,
        "repository": repository,
        "project_root": str(root),
        "state_dir": str(state),
        "publish": True,
    }
    atomic_json(config_path, config)
    command = f"python3 {helpers / 'record_update.py'} --config {config_path}"
    guidance = (
        f"For implementation, verification or deployment that changes files under `{root}`, "
        f"load `$eldheim-devlog` from `{skill / 'SKILL.md'}`. Before the final task reply, record "
        f"the actual completed changes, checks and deployment scope using the collector. "
        f"The owner has authorized automatic publication of curated task summaries to `{repository}`. "
        "Skip read-only questions and unrelated projects. Never publish raw logs, conversations, "
        "secrets, personal identifiers or local paths. Follow the skill's status/evidence rules. "
        f"Collector: `{command} add ...`; pending records are retained for retry. "
        "This logging authorization does not authorize additional game-server changes."
    )
    managed_block(codex_home / "AGENTS.md", guidance)
    managed_block(root / "AGENTS.md", guidance)
    scoped_skill = root / ".agents/skills/eldheim-devlog"
    scoped_skill.parent.mkdir(parents=True, exist_ok=True)
    if scoped_skill.is_symlink():
        if scoped_skill.resolve() != skill.resolve():
            raise ValueError("Project skill alias points elsewhere")
    elif not scoped_skill.exists():
        scoped_skill.symlink_to(skill, target_is_directory=True)
    if timer:
        service_dir = user_home / ".config/systemd/user"
        service_dir.mkdir(parents=True, exist_ok=True)
        exe = shutil.which("python3") or sys.executable
        args = [exe, str(helpers / "record_update.py"), "--config", str(config_path), "flush"]
        service = (
            "[Unit]\nDescription=Publish curated Eldheim devlog queue\n\n[Service]\nType=oneshot\n"
        )
        service += (
            "ExecStart="
            + " ".join(map(systemd_argument, args))
            + "\nTimeoutStartSec=180\nUMask=0077\n"
        )
        timer_text = (
            "[Unit]\nDescription=Retry Eldheim devlog publication\n\n[Timer]\n"
            "OnBootSec=2min\nOnUnitActiveSec=5min\nRandomizedDelaySec=20s\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )
        (service_dir / "eldheim-devlog-publish.service").write_text(service)
        (service_dir / "eldheim-devlog-publish.timer").write_text(timer_text)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "eldheim-devlog-publish.timer"], check=True
        )
    return {
        "repository": repository,
        "config": str(config_path),
        "skill": str(skill),
        "timer": timer,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("--no-timer", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install(
            args.repository,
            args.project_root,
            timer=not args.no_timer,
            user_home=Path.home(),
            config_path=args.config.expanduser(),
            codex_home=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as failure:
        print(f"Install: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
