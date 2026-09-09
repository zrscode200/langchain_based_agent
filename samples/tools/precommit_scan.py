#!/usr/bin/env python3
"""Scan STAGED added lines for content that must not be committed. Exit 1 blocks.

    git add -A && python3 tools/precommit_scan.py && git commit ...

WHY THIS IS A FILE AND NOT A SHELL ONE-LINER
--------------------------------------------
Twice in one session an inline scan reported a problem and the commit happened anyway,
because the chain was `git add … && <scan> && git commit …` written so that the commit
depended on `git add`, not on the scan's exit status. And once before that, a shell scan
silently produced EMPTY output — matching nothing because the pipeline was wrong, not
because the diff was clean — and read as a pass.

So this script has two jobs, and the second is the one that keeps failing:

1. find the patterns
2. **exit non-zero when it finds them, and exit non-zero when it cannot tell**

An empty diff is an ABORT, not a pass. A scan that scans nothing is the most dangerous
possible outcome, because it looks exactly like success.

WHAT IT LOOKS FOR, and why each one is here rather than being generic:

* the live MUSE SESSION value, read from the cookie file and never printed
* other programs' compound identifiers — this workspace is scoped to ONE program and
  cross-program material must not leak into its records
* proprietary markers — meds documents declare a data classification in a `sensitivity`
  field, and their text must never be committed. (Described rather than quoted here on
  purpose: a scanner whose own documentation trips it would have to exempt itself, and a
  scanner with an exemption has a hole exactly where someone will later put a real
  finding.)
* personal names — facet terms on `scited` and `signals` `author.raw` are people
* ISIDs — `user-access-check` and `FacetResultsV1.user` both return the caller's

Findings are reported for review rather than auto-approved: some are false positives (a
base64 JSON prefix looks like a JWT), and the point is to make a human look, not to make
the check pass.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

def _repo_root() -> pathlib.Path:
    """The git root, asked for rather than counted to.

    This was `parents[2]`, which encoded how deep the workspace happened to sit inside its
    repository -- correct while the workspace was `<repo>/tt8/`, and wrong the moment it became
    the repository root itself: `parents[2]` then resolved to the directory ABOVE the repo, and
    `git -C` there found nothing staged. The scan aborted rather than passing, which is the
    behaviour this file exists to have, but it was unusable until the depth was corrected. A
    depth constant cannot survive a relocation; asking git can.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(pathlib.Path(__file__).resolve().parent), "rev-parse",
             "--show-toplevel"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        # No git, or not a repository. Fall back to the workspace root -- the diff will then
        # come back empty and the abort below is the correct outcome, not a silent pass.
        return pathlib.Path(__file__).resolve().parents[1]
    return pathlib.Path(out) if out else pathlib.Path(__file__).resolve().parents[1]


REPO = _repo_root()

# Adjust when the program changes. Deliberately explicit rather than inferred: a wrong
# inference here fails open.
PROGRAM_MK = "MK-6070"
PROGRAM_MCODE = "M0060070"

PATTERNS: dict[str, str] = {
    "other program's MK number": rf"\bMK-(?!{PROGRAM_MK[3:]}\b)\d{{4}}\b",
    "other program's M-code": rf"\bM00600(?!{PROGRAM_MCODE[6:]}\b)\d\d\b",
    "proprietary marker": r"(?i)\bsensitivity\s*[:=]\s*[\"']?Proprietary",
    "JWT-shaped literal": r"eyJ[A-Za-z0-9_-]{20,}",
    "ISID-shaped assignment": r"(?i)\b(isid|user)\s*[:=]\s*[\"'][a-z]{4,6}\d{1,3}[\"']",
    "known personal-name facet term": r"\bMIGRATED\b",
    "hardcoded bearer": r"(?i)Bearer\s+[A-Za-z0-9_\-.]{16,}",
    "hardcoded SESSION cookie": r"(?i)Cookie\s*:\s*SESSION=[A-Za-z0-9_./%+\-=]{16,}",
}
SECRET_FINDINGS = {"JWT-shaped literal", "hardcoded bearer", "hardcoded SESSION cookie"}


def session_fragment() -> str | None:
    """The live SESSION value's first 12 chars, for a containment test. Never printed."""
    default = (
        pathlib.Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")))
        / "ics-companion" / "secrets" / "muse-qa-session"
    )
    p = pathlib.Path(os.environ.get("ICS_MUSE_SESSION_FILE", str(default)))
    try:
        session = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return session[:12] if len(session) >= 12 else None


def main() -> int:
    diff = subprocess.run(["git", "-C", str(REPO), "diff", "--cached", "-U0"],
                          capture_output=True, text=True).stdout
    added = [ln[1:] for ln in diff.splitlines()
             if ln.startswith("+") and not ln.startswith("+++")]

    if not added:
        # THE IMPORTANT BRANCH. Nothing staged, or the diff could not be read. Either way
        # the scan established nothing, and "established nothing" must not read as "clean".
        print("SCAN ABORTED: no staged added lines found.", file=sys.stderr)
        print("  Stage your changes first. A scan over nothing is not a pass.",
              file=sys.stderr)
        return 2

    findings: list[tuple[str, str]] = []
    for line in added:
        for name, pat in PATTERNS.items():
            if re.search(pat, line):
                report = "<line withheld>" if name in SECRET_FINDINGS else line.strip()
                findings.append((name, report))

    frag = session_fragment()
    if frag:
        for line in added:
            if frag in line:
                findings.append(("THE LIVE MUSE SESSION COOKIE", "<line withheld>"))
    else:
        print("note: no readable SESSION file, so the cookie-containment test did not run",
              file=sys.stderr)

    print(f"scanned {len(added)} staged added lines against {len(PATTERNS)} patterns"
          + (" + the live SESSION cookie" if frag else ""))
    if not findings:
        print("SCAN CLEAN")
        return 0

    seen: set[tuple[str, str]] = set()
    for name, line in findings:
        key = (name, line[:80])
        if key in seen:
            continue
        seen.add(key)
        print(f"  HIT [{name}] {line[:110]}", file=sys.stderr)
    print(f"\nSCAN FAILED: {len(seen)} distinct finding(s). Review each, then either fix "
          f"it or re-run with --accept if it is genuinely a false positive.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if "--accept" in sys.argv:
        # Deliberately noisy: accepting a finding should be visible in the terminal
        # history of whoever did it.
        print("WARNING: --accept was passed. Findings below are being ACKNOWLEDGED, "
              "not fixed.", file=sys.stderr)
        rc = main()
        sys.exit(0 if rc in (0, 1) else rc)
    sys.exit(main())
