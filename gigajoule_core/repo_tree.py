"""Which files the hygiene checks walk, and where that list comes from.

Role: code hygiene
Reads: the git index, or the filesystem when git is unavailable
Writes: nothing
Can move tokens: no
Live-safe: yes

THE DENOMINATOR IS THE GIT INDEX, NOT THE WORKING TREE, and saying which is the
whole reason this module exists rather than a `Path.rglob` at each call site.
A count without its denominator cannot be compared to anything measured later,
and a walk of the working tree silently includes whatever happens to be lying
around -- a scratch file, a half-finished experiment, a virtualenv somebody
created inside the repo -- so two runs an hour apart can disagree for reasons
that have nothing to do with the code.

`enumeration_mode()` reports which source was used, so any figure derived from
this walk can state its method alongside its number.

EXTENSIONLESS SCRIPTS COUNT AS PYTHON FILES, and that is not a detail. This
repo's entry points live at the project root without a `.py` suffix, because an
operator types `./gigajoule` rather than `./gigajoule.py`. A walk that globbed
only `*.py` would therefore exempt precisely the files a human runs -- the ones
whose `Can move tokens` and `Live-safe` declarations matter most -- from every
hygiene check in the suite. So the walk also takes any tracked file with no
suffix whose first line is a Python shebang.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


def enumeration_mode() -> str:
    """`git-index` or `filesystem-walk`. Print this next to any count."""
    return "git-index" if _git_files() is not None else "filesystem-walk"


def _shebang_is_python(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            first = handle.readline()
    except OSError:
        return False
    return first.startswith("#!") and "python" in first


def _git_files() -> list[Path] | None:
    """Every tracked Python file per git, or None when git cannot answer.

    The subprocess call takes a fixed argument list with no shell and no value
    derived from input, which is what the two suppressions below assert. They are
    the only suppressions in this repo: the alternative to asking git is walking
    the filesystem, and that would make the denominator "whatever happened to be
    lying around", which is the thing this module exists to avoid.
    """
    try:
        out = subprocess.run(  # noqa: S603 -- fixed argv, shell=False, nothing interpolated
            ["git", "-C", str(REPO_ROOT), "ls-files"],  # noqa: S607 -- git resolved from PATH by design; pinning an absolute path would break every machine but one
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    tracked = [REPO_ROOT / line for line in out.splitlines() if line.strip()]
    return [p for p in tracked if p.suffix == ".py" or _shebang_is_python(p)]


def repo_python_files() -> list[Path]:
    """Every Python file the hygiene checks apply to, sorted for stable output."""
    tracked = _git_files()
    if tracked is not None:
        return sorted(p for p in tracked if p.exists())
    walked = [
        p
        for p in REPO_ROOT.rglob("*")
        if p.is_file() and not _SKIP_DIRS.intersection(p.relative_to(REPO_ROOT).parts)
    ]
    return sorted(p for p in walked if p.suffix == ".py" or _shebang_is_python(p))
