# MUSE QA authentication

MUSE QA now authenticates these direct API calls with the browser's `SESSION`
cookie. The retired `/oauth_token` and refresh-token flow is not used.

## What changed from OAuth

This is a change from an OAuth Bearer token to a browser session cookie. The
`SESSION` value is a cookie credential, not an OAuth access token.

| Before: OAuth | Now: SESSION |
|---|---|
| Sent `Authorization: Bearer <access-token>` | Sends `Cookie: SESSION=<value>` |
| Stored access and refresh tokens | Stores only the `SESSION` value |
| Called `/resources/v1/token` to refresh | Makes a read-only GET every 30 minutes to keep the session active |
| A 20-second watcher rotated tokens | `muse_session.py serve` rereads the cookie file on every check |
| A dead refresh token required a new OAuth capture | An expired SESSION requires copying a new browser cookie |

The migration removed `muse_token.py`, `get-muse-qa-token.sh`,
`muse-qa-token`, `muse-qa-refresh-token`, client credentials, and refresh-grant
handling. Current callers read `muse-qa-session`; the environment override is
`ICS_MUSE_SESSION_FILE`.

Existing MUSE request URLs and JSON bodies do not change. Only the
authentication header and credential lifecycle change.

## Capture the cookie

From the `product-companion` repository root:

```sh
python3 auth/muse_session.py capture
```

A small macOS window opens. In browser developer tools, find the MUSE QA cookie
named `SESSION`, paste either its value, `SESSION=<value>`, or the complete
Cookie header, and select **Save**. The value is hidden while pasted. The tool
keeps only `SESSION`, saves it outside the repository with mode `0600`, and
immediately checks it against MUSE.

The default file is:

```text
$XDG_CONFIG_HOME/ics-companion/secrets/muse-qa-session
```

Set `ICS_MUSE_SESSION_FILE` to override it.

## Keep it active

The SESSION reportedly expires after three hours without activity. Keep this
process running while using the companion:

```sh
python3 auth/muse_session.py serve
```

It makes one read-only validating GET every 30 minutes:

- `200`: active; wait 30 minutes.
- `401`: expired or invalid; keep watching for a replacement. Run `capture` in
  another terminal or after stopping the process.
- `429`, `5xx`, or transport failure: retain the cookie and retry with capped
  backoff.
- another HTTP error: stop without claiming the cookie expired.

The keepalive can preserve an active server session; it cannot renew an expired
one. Whether MUSE resets its inactivity timer on this GET is an operational
assumption to confirm over a full three-hour window.

## One-off commands

```sh
python3 auth/muse_session.py check
python3 auth/muse_session.py clear
```

`check` uses `GET /resources/v1/data-updates`, the measured authentication
probe. `/version` and `/health` are not valid probes. `clear` removes only the
saved local cookie.

The `tt8` connector rereads the cookie file for every request and sends:

```http
Cookie: SESSION=<saved value>
```

Neither the utility nor the connector prints the value.
