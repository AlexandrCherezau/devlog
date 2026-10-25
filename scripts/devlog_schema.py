"""Shared public record format. No transcripts, local paths or credentials."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

STATUSES = {
    "implemented": "Реализовано, ещё не выложено",
    "tested": "Проверено локально, ещё не выложено",
    "deployed-test": "Установлено на тестовом сервере",
    "deployed-prod": "Установлено на основном сервере",
}
FIELDS = {
    "version",
    "id",
    "created_at",
    "project",
    "title",
    "changes",
    "status",
    "verification",
    "notes",
    "source_revision",
}
SENSITIVE = re.compile(
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]{15,}|sk-[A-Za-z0-9_-]{20,})|"
    r"\b\d{6,}:[A-Za-z0-9_-]{25,}|"
    r"https?://[^\s/]+@|\bBearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|"
    r"(?<![\w:])/(?:home|root|Users|srv|opt|var|etc|tmp|mnt|data|run|private)(?:/|\b)|"
    r"~/|[A-Za-z]:\\|"
    r"\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
    r"[\w.-]*[\"'`]?\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def public_text(value: object, field: str, limit: int = 1500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field}: expected nonempty text of at most {limit} characters")
    if any(ord(char) < 32 for char in value) or SENSITIVE.search(value):
        raise ValueError(
            f"{field}: contains control characters, a private path or credential-like text"
        )
    return value.strip()


def validate_record(record: object) -> dict:
    if not isinstance(record, dict) or set(record) - FIELDS:
        raise ValueError("Unknown record format/fields")
    if not (FIELDS - {"source_revision"}) <= set(record):
        raise ValueError("Record is missing required fields")
    if type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("Unsupported record version")
    if not isinstance(record["id"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{7,79}", record["id"]
    ):
        raise ValueError("Invalid record id")
    if not isinstance(record["project"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,59}", record["project"]
    ):
        raise ValueError("Invalid project slug")
    if not isinstance(record["status"], str) or record["status"] not in STATUSES:
        raise ValueError("Unsupported status")
    try:
        created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        if created.utcoffset() is None:
            raise ValueError("Timestamp needs a timezone")
    except (TypeError, AttributeError, ValueError) as failure:
        raise ValueError("Invalid created_at timestamp") from failure
    public_text(record["title"], "title", 180)
    for field in ("changes", "verification", "notes"):
        values = record[field]
        if not isinstance(values, list) or len(values) > 30 or (field == "changes" and not values):
            raise ValueError(f"{field}: expected a list of at most 30 strings")
        for value in values:
            public_text(value, field)
    if "source_revision" in record and (
        not isinstance(record["source_revision"], str)
        or not re.fullmatch(r"[0-9a-f]{7,64}", record["source_revision"])
    ):
        raise ValueError("source_revision must be a Git revision, not a path or URL")
    return record


def canonical_json(record: dict) -> str:
    validate_record(record)
    return json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def fingerprint(record: dict) -> str:
    facts = {key: value for key, value in record.items() if key not in {"id", "created_at"}}
    return hashlib.sha256(
        json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def render_record(record: dict) -> str:
    validate_record(record)
    day = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")).date().isoformat()
    lines = [
        f"## {day} — {record['project']}: {record['title']}",
        "",
        f"**Состояние:** {STATUSES[record['status']]}",
        "",
    ]
    lines += ["- " + item for item in record["changes"]]
    for field, heading in (("verification", "Проверка"), ("notes", "Ограничения и заметки")):
        if record[field]:
            lines += ["", f"**{heading}:**"] + ["- " + item for item in record[field]]
    if record.get("source_revision"):
        lines += ["", "Исходный коммит: `" + record["source_revision"] + "`."]
    lines += ["", "Идентификатор записи: `" + record["id"] + "`."]
    return "\n".join(lines) + "\n"
