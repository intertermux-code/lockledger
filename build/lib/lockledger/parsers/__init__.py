"""Lockfile format detection and dispatch."""

import json
from pathlib import Path

from ..models import PackageRecord, read_text
from .npm import parse_npm_lock
from .pip import parse_requirements
from .uv import parse_uv_lock

FORMATS = ("npm", "pip", "uv")


class FormatError(ValueError):
    """The file is not (or not clearly) a supported lockfile."""


def detect_format(path: Path, text: str) -> str:
    name = path.name.lower()
    if name in ("package-lock.json", "npm-shrinkwrap.json"):
        return "npm"
    if name == "uv.lock":
        return "uv"
    if name.endswith(".lock"):
        if "[[package]]" in text:
            return "uv"
        raise FormatError(f"cannot identify lockfile format for {path}; pass --format")
    if name.endswith((".txt", ".in")) or "requirements" in name:
        return "pip"
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return "pip"
        if isinstance(data, dict) and "lockfileVersion" in data:
            return "npm"
    if "[[package]]" in text:
        return "uv"
    return "pip"


def parse_lockfile(path: Path, lock_format: str | None = None,
                   notes: list[str] | None = None) -> list[PackageRecord]:
    text = read_text(path)
    kind = (lock_format or detect_format(path, text)).lower()

    if kind not in FORMATS:
        expected = ", ".join(FORMATS)
        raise FormatError(f"unknown lockfile format '{kind}', expected one of: {expected}")

    try:
        if kind == "npm":
            records = parse_npm_lock(text)
        elif kind == "pip":
            records = parse_requirements(text, notes)
        else:
            records = parse_uv_lock(path.read_bytes(), notes)
    except FormatError:
        raise
    except ValueError as error:  # malformed JSON/TOML from a parser
        raise FormatError(str(error)) from error

    if not records:
        raise FormatError(f"{path} contained no pinned packages as {kind}")
    return records
