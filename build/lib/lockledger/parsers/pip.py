"""requirements.txt / pip freeze parser (== pins and direct references)."""

import re

from ..models import PackageRecord

NAME_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?")
DIRECT_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*@\s*(?P<url>\S+)")


def _strip_comment(line: str) -> str:
    """Drop an unquoted # trailing comment."""
    quote = ""
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = ""
        elif character in ("'", '"'):
            quote = character
        elif character == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _logical_lines(text: str):
    """Join backslash continuations and drop comments/blank lines."""
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        buffer += line
        stripped, buffer = _strip_comment(buffer).strip(), ""
        if stripped and not stripped.startswith("#"):
            yield stripped
    stripped = _strip_comment(buffer).strip()
    if stripped and not stripped.startswith("#"):
        yield stripped


def _split_hashes(line: str) -> tuple[str, list[str]]:
    """Separate a requirement from the --hash=... tokens trailing it."""
    requirement = []
    hashes = []
    tokens = line.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--hash" and index + 1 < len(tokens):
            hashes.append(tokens[index + 1].replace(":", "=", 1))
            index += 2
            continue
        if token.startswith("--hash"):
            hashes.append(token.split("=", 1)[1].replace(":", "=", 1))
            index += 1
            continue
        requirement.append(token)
        index += 1
    return " ".join(requirement), hashes


def parse_requirements(text: str, notes: list[str] | None = None) -> list[PackageRecord]:
    records: list[PackageRecord] = []

    def warn(message: str):
        if notes is not None:
            notes.append(message)

    for logical in _logical_lines(text):
        line, hashes = _split_hashes(logical)
        integrity = ";".join(hashes)

        if line.startswith("-"):
            option = line.split(" ", 1)[0]
            if option == "-e":
                # An editable install points at a live checkout, so there is
                # no published artifact to record.
                warn(f"skipped editable requirement: {line}")
            else:
                # -r/-c/--index-url describe the resolution environment, not
                # a resolved artifact.
                warn(f"skipped option line: {logical}")
            continue

        direct = DIRECT_RE.match(line)
        if direct:
            records.append(
                PackageRecord(
                    name=direct.group("name").lower(),
                    version="unpinned",
                    ecosystem="pypi",
                    resolved_url=direct.group("url"),
                    integrity=integrity,
                )
            )
            continue

        name_match = NAME_RE.match(line)
        if not name_match:
            warn(f"unparseable requirement line: {logical}")
            continue
        name = name_match.group("name").lower()
        remainder = line[name_match.end():].split(";", 1)[0].strip()

        if remainder.startswith("=="):
            records.append(
                PackageRecord(
                    name=name,
                    version=remainder[2:].strip(),
                    ecosystem="pypi",
                    integrity=integrity,
                )
            )
        else:
            warn(f"unpinned requirement skipped: {name} {remainder}".strip())

    return records
