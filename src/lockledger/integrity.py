"""Subresource-integrity style checks on downloaded artifacts."""

import base64
import hashlib

ALGORITHMS = {
    "sha256": hashlib.sha256,
    "sha384": hashlib.sha384,
    "sha512": hashlib.sha512,
}


def parse(integrity: str) -> list[tuple[str, str]]:
    """Split "sha512-abc==" / "sha256=deadbeef" into (algorithm, expected) pairs.

    npm writes base64 after a dash, pip and uv write hex after "=" or ":";
    base64 never contains a dash, so the first separator wins.
    """
    pairs = []
    for token in integrity.replace(";", " ").split():
        positions = [token.find(sep) for sep in ("-", "=", ":") if token.find(sep) != -1]
        if not positions:
            continue
        cut = min(positions)
        algorithm, expected = token[:cut].lower(), token[cut + 1:]
        if algorithm in ALGORITHMS and expected:
            pairs.append((algorithm, expected))
    return pairs


def check(payload: bytes, integrity: str) -> tuple[bool, str]:
    """True when every recorded hash matches; the note explains what did not."""
    pairs = parse(integrity)
    for algorithm, expected in pairs:
        hasher = ALGORITHMS[algorithm](payload)
        candidates = {hasher.hexdigest(), base64.b64encode(hasher.digest()).decode("ascii")}
        if expected not in candidates:
            return False, f"{algorithm} mismatch: artifact does not match {expected}"
    if integrity and not pairs:
        return True, f"no supported hash in '{integrity}', verification skipped"
    return True, ""
