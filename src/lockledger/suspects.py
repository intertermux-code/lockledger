"""Suspects heuristics: typosquatting detection and install script diffing."""

from __future__ import annotations

import importlib.resources as importlib_resources
from pathlib import Path
from typing import Optional

from .difftool import NPM_INSTALL_SCRIPTS, _find, _npm_scripts


def _load_popular_names() -> set:
    """Load bundled popular package names (offline, stdlib only)."""
    try:
        data_path = importlib_resources.files("lockledger") / "data" / "popular_names.txt"
    except Exception:
        data_path = Path(__file__).parent / "data" / "popular_names.txt"
    if not data_path.is_file():
        return set()
    names = set()
    for raw in data_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lower()
        if line and not line.startswith("#"):
            names.add(line)
    return names


POPULAR_NAMES = _load_popular_names()


def levenshtein_distance(a: str, b: str) -> int:
    """Compute Levenshtein edit distance (stdlib only)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if len(b) == 0:
        return len(a)
    previous_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a):
        current_row = [i + 1]
        for j, char_b in enumerate(b):
            insertions = current_row[j] + 1
            deletions = previous_row[j + 1] + 1
            substitutions = previous_row[j] + (char_a != char_b)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def is_typosquat(name: str, popular_names: Optional[set[str]] = None) -> bool:
    """Check if name is a typosquat of a popular package (not exact match)."""
    if popular_names is None:
        popular_names = POPULAR_NAMES
    name_lc = name.lower()
    # Exact match must NOT be flagged
    if name_lc in popular_names:
        return False
    # Flag close variants
    for popular in popular_names:
        if abs(len(name_lc) - len(popular)) > 2:
            continue
        dist = levenshtein_distance(name_lc, popular)
        if dist == 1:
            return True
        if dist == 2:
            return True  # flag obvious typos like lodahs vs lodash
    return False


def find_typosquats_in_names(names: list[str], popular_names: Optional[set[str]] = None) -> list[dict]:
    """Find typosquat suspects from a list of package names."""
    if popular_names is None:
        popular_names = POPULAR_NAMES
    suspects = []
    seen = set()
    for name in names:
        name_lc = name.lower()
        if name_lc in seen:
            continue
        seen.add(name_lc)
        if is_typosquat(name, popular_names):
            # Find closest popular match
            best_match = None
            best_dist = float('inf')
            for popular in popular_names:
                if abs(len(name_lc) - len(popular)) > 3:
                    continue
                dist = levenshtein_distance(name_lc, popular)
                if dist < best_dist:
                    best_dist = dist
                    best_match = popular
            suspects.append({
                "name": name,
                "suspected_target": best_match,
                "distance": best_dist,
            })
    return suspects


def diff_install_hooks(before_artifact, after_artifact) -> dict:
    """Flag new or changed install hooks prominently (npm focus)."""
    old_scripts, new_scripts = _npm_scripts(before_artifact), _npm_scripts(after_artifact)
    added = sorted(hook for hook in new_scripts if hook not in old_scripts)
    removed = sorted(hook for hook in old_scripts if hook not in new_scripts)
    changed = sorted(
        hook for hook in new_scripts
        if hook in old_scripts and new_scripts[hook] != old_scripts[hook]
    )
    flags = []
    for hook in added:
        flags.append(f"NEW install script {hook}: {new_scripts[hook]}")
    for hook in changed:
        flags.append(f"CHANGED install script {hook}: {old_scripts[hook]} -> {new_scripts[hook]}")
    for hook in removed:
        flags.append(f"REMOVED install script {hook}: {old_scripts[hook]}")
    return {
        "added": [{"hook": hook, "command": new_scripts[hook]} for hook in added],
        "removed": [{"hook": hook, "command": old_scripts[hook]} for hook in removed],
        "changed": [
            {"hook": hook, "before": old_scripts[hook], "after": new_scripts[hook]}
            for hook in changed
        ],
        "flags": flags,
        "has_changes": bool(added or removed or changed),
    }
