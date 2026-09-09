#!/usr/bin/env python3
"""Capture, validate, and keep alive a MUSE QA SESSION cookie."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

DEFAULT_SESSION_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    / "ics-companion"
    / "secrets"
    / "muse-qa-session"
)
DEFAULT_CHECK_URL = "https://qa.muse.merck.com/resources/v1/data-updates"
DEFAULT_INTERVAL = 1800
RECAPTURE_INTERVAL = 60
MAX_BACKOFF = 300
CLIENT_APP = "ics-ddt-companion"


def session_file() -> Path:
    configured = os.environ.get("ICS_MUSE_SESSION_FILE")
    return Path(configured).expanduser() if configured else DEFAULT_SESSION_FILE


def check_url() -> str:
    return os.environ.get("ICS_MUSE_SESSION_CHECK_URL", DEFAULT_CHECK_URL)


def normalize_session(value: str) -> str:
    candidate = value.strip()
    if candidate.lower().startswith("cookie:"):
        candidate = candidate[7:].strip()
    if ";" in candidate:
        cookies = {}
        for item in candidate.split(";"):
            name, separator, cookie_value = item.strip().partition("=")
            if separator:
                cookies[name.strip().upper()] = cookie_value.strip()
        if "SESSION" not in cookies:
            raise ValueError("The pasted Cookie header does not contain SESSION.")
        candidate = cookies["SESSION"]
    elif candidate.upper().startswith("SESSION="):
        candidate = candidate[8:]
    if len(candidate) < 16 or any(char.isspace() or ord(char) < 32 for char in candidate):
        raise ValueError("The SESSION value is empty or malformed.")
    return candidate


def capture_from_dialog() -> str:
    if sys.platform != "darwin":
        raise RuntimeError("The secure capture window is available only on macOS.")
    script = """
set promptText to "Paste the MUSE QA SESSION cookie."
set detailText to "Paste the SESSION value, SESSION=value, or the complete Cookie header. Only SESSION is kept. The input is hidden and saved only in your local secrets directory."
try
    set resultDialog to display dialog (promptText & return & return & detailText) default answer "" with hidden answer buttons {"Cancel", "Save"} default button "Save" cancel button "Cancel" with title "MUSE QA session"
    return text returned of resultDialog
on error number -128
    return "__MUSE_CAPTURE_CANCELLED__"
end try
"""
    result = subprocess.run(
        ["/usr/bin/osascript"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not open the capture window.")
    if result.stdout.strip() == "__MUSE_CAPTURE_CANCELLED__":
        raise KeyboardInterrupt
    return normalize_session(result.stdout)


def capture_from_stdin() -> str:
    if sys.stdin.isatty():
        raise RuntimeError("--stdin expects the SESSION value on standard input.")
    return normalize_session(sys.stdin.read())


def write_session(value: str, path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def read_session(path: Path) -> str:
    try:
        return normalize_session(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"No SESSION cookie is saved at {path}.") from exc
    except PermissionError as exc:
        raise RuntimeError(f"Cannot read the SESSION cookie at {path}.") from exc


def _curl_config(session: str, url: str) -> str:
    def quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        "silent",
        "show-error",
        "output = /dev/null",
        f'url = "{quote(url)}"',
        f'header = "Accept: application/json"',
        f'header = "X-Client-App: {CLIENT_APP}"',
        f'header = "Cookie: SESSION={quote(session)}"',
        'write-out = "%{http_code}"',
        "connect-timeout = 15",
        "max-time = 60",
    ]
    ca_bundle = os.environ.get("ICS_CA_BUNDLE")
    if ca_bundle:
        lines.append(f'cacert = "{quote(str(Path(ca_bundle).expanduser()))}"')
    return "\n".join(lines) + "\n"


def request_status(path: Path) -> int | None:
    session = read_session(path)
    result = subprocess.run(
        ["/usr/bin/curl", "-q", "--config", "-"],
        input=_curl_config(session, check_url()),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"curl exited {result.returncode}"
        print(f"transport failure: {detail}", file=sys.stderr)
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        print("MUSE returned an unreadable HTTP status.", file=sys.stderr)
        return None


def describe_status(status: int | None) -> tuple[str, bool]:
    if status == 200:
        return "ACTIVE", True
    if status == 401:
        return "EXPIRED OR INVALID", False
    if status == 429 or (status is not None and 500 <= status <= 599):
        return f"TEMPORARILY UNAVAILABLE (HTTP {status})", False
    if status is None:
        return "TEMPORARILY UNAVAILABLE", False
    return f"ERROR (HTTP {status})", False


def check_once(path: Path) -> int:
    try:
        status = request_status(path)
    except (RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    label, active = describe_status(status)
    print(label)
    if active:
        return 0
    if status == 401:
        print("Run `python3 auth/muse_session.py capture` to replace it.", file=sys.stderr)
        return 2
    return 1


def capture(path: Path, use_stdin: bool) -> int:
    try:
        value = capture_from_stdin() if use_stdin else capture_from_dialog()
        write_session(value, path)
    except KeyboardInterrupt:
        print("Capture cancelled.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Capture failed: {exc}", file=sys.stderr)
        return 2
    print(f"Saved the SESSION cookie to {path} with mode 0600.")
    return check_once(path)


def serve(path: Path, interval: int) -> int:
    if interval < 60:
        print("--interval must be at least 60 seconds.", file=sys.stderr)
        return 2
    print(
        f"Keeping the MUSE QA SESSION active every {interval}s. "
        f"Cookie: {path}. Ctrl-C to stop."
    )
    backoff = 30
    while True:
        timestamp = datetime.now().strftime("%H:%M:%S")
        try:
            status = request_status(path)
        except (RuntimeError, ValueError) as exc:
            print(f"{timestamp}  WAITING — {exc}")
            delay = RECAPTURE_INTERVAL
        else:
            label, active = describe_status(status)
            print(f"{timestamp}  {label}")
            if active:
                delay = interval
                backoff = 30
            elif status == 401:
                print(
                    "The keepalive cannot renew this cookie. Run "
                    "`python3 auth/muse_session.py capture`; this process "
                    "will detect the replacement."
                )
                delay = RECAPTURE_INTERVAL
            elif status == 429 or status is None or 500 <= status <= 599:
                delay = backoff
                backoff = min(backoff * 2, MAX_BACKOFF)
                print(f"Retrying in {delay}s; the saved cookie was not changed.")
            else:
                print(
                    "Stopping because MUSE rejected the validating request "
                    "for a reason other than authentication.",
                    file=sys.stderr,
                )
                return 1
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def clear(path: Path) -> int:
    try:
        path.unlink()
    except FileNotFoundError:
        print(f"No saved SESSION cookie at {path}.")
        return 0
    except OSError as exc:
        print(f"Could not remove {path}: {exc}", file=sys.stderr)
        return 1
    print(f"Removed {path}.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture, validate, and keep alive a MUSE QA SESSION cookie."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture", help="open a secure capture window")
    capture_parser.add_argument(
        "--stdin",
        action="store_true",
        help="read the cookie from stdin instead of opening a window",
    )
    subparsers.add_parser("check", help="make one validating MUSE GET")
    serve_parser = subparsers.add_parser(
        "serve", help="keep the SESSION active and watch for replacements"
    )
    serve_parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"seconds between successful checks (default: {DEFAULT_INTERVAL})",
    )
    subparsers.add_parser("clear", help="remove the locally saved SESSION cookie")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    path = session_file()
    if args.command == "capture":
        return capture(path, args.stdin)
    if args.command == "check":
        return check_once(path)
    if args.command == "serve":
        return serve(path, args.interval)
    return clear(path)


if __name__ == "__main__":
    raise SystemExit(main())
