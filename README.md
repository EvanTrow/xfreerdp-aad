# xfreerdp-aad

Wrapper around `xfreerdp` that automates Azure AD (Entra ID) login so you
don't have to manually click through the certificate prompt and the
browser-based OAuth flow every time.

## How it works

`xfreerdp /sec:aad` doesn't do a simple username/password prompt. It:

1. Optionally asks you to trust the server's TLS certificate
   (`Do you trust the above certificate? (Y/T/N)`).
2. Prints an OAuth2 authorize URL (`Browse to: https://login.microsoftonline.com/...`).
3. Blocks on stdin with `Paste redirect URL here:`, waiting for you to log
   in via that URL in a browser and paste back the URL it redirects to
   (which fails to load - that's expected, you just need the URL).

[aad_login_bot.py](aad_login_bot.py) spawns `xfreerdp` under a pty
(`pexpect`), watches for those prompts, and:

- Passes `/cert:tofu` on the command line so the cert prompt is normally
  skipped entirely (trust-on-first-use: auto-accepted the first time you
  connect to a host, and auto-*rejected* - not silently bypassed - if the
  certificate ever changes afterward, which is what you want if a MITM
  shows up). If an interactive cert prompt somehow still appears, the bot
  does **not** auto-answer it - it hands the terminal to you so a real
  mismatch can't be rubber-stamped.
- Opens the AAD authorize URL in a real (visible, by default) Chromium
  browser via Playwright and drives the Microsoft login form.
- Pulls the username, password, and current TOTP code from the 1Password
  CLI (`op`) at the moment they're needed - nothing is stored on disk.
- Reads the resulting redirect URL back out of the browser's address bar
  and feeds it to `xfreerdp`'s stdin.

## Setup

1. **1Password CLI**: install `op` and enable "Integrate with 1Password
   CLI" in the desktop app (Settings > Developer) so `op` commands work
   without a manual `op signin`. Confirm with:
   ```
   op item get <item name or UUID> --format json --reveal
   ```
2. **1Password item**: create/use an item with:
   - a Username field (purpose `USERNAME`)
   - a Password field (purpose `PASSWORD`)
   - a field labeled exactly `one-time password`, set up from the *same*
     QR code/secret you gave to Microsoft Authenticator, so 1Password
     generates matching codes

   The item is always referenced by UUID (required argument, no default -
   nothing about which item to use is hardcoded in the script), so a
   rename or a vault's display-name quirks (see below) can't silently
   break the lookup. Find an item's UUID with:
   ```
   op item get "<item name>" --format json | jq -r .id
   ```
3. **Python environment**:
   ```
   ./setup.sh
   ```
   This creates `.venv/`, installs `pexpect` + `playwright`, and downloads
   Playwright's bundled Chromium.

## Usage

Both the host and the 1Password item UUID are required - there's no default:

```
./rdp-connect.sh <host> <1pass_item_uuid>
```

Extra `xfreerdp` flags can be appended after that (in any order relative to
`--desktop`) and are passed straight through:

```
./rdp-connect.sh <host> <1pass_item_uuid> /w:1920 /h:1080
```

Optionally move the RDP window to a virtual desktop/workspace once it opens
(equivalent to your `wmctrl -s <index>` Stream Deck shortcut, so it's handy
to bind this whole command to a Stream Deck/OpenDeck button too):

```
./rdp-connect.sh <host> <1pass_item_uuid> --desktop 1
```

This works by giving the session's xfreerdp window a distinctive
`/wm-class:` (derived from the host) and polling `wmctrl -x -r <class> -t
<desktop>` for up to 2 minutes until the window appears, then switching to
that desktop with `wmctrl -s <desktop>`. It runs in the background and
never blocks or fails the connection - if `wmctrl` isn't installed or the
window never shows up, it just logs a message and moves on.

Env vars:

- `RDP_AAD_HEADLESS=1` - run the login browser headless instead of visibly
  (visible is the default so you can see/intervene if Microsoft shows an
  unexpected screen, e.g. Conditional Access or a compliance check)

## Notes / limitations

- Assumes MFA is a straight 6-digit TOTP prompt. If your tenant defaults to
  a push notification instead, the bot looks for a "use a verification
  code instead" link and switches automatically; if the wording differs it
  will surface an error rather than hang.
- The login browser uses a fresh, non-persistent context each run - no
  cookies or session state survive between connections.
- If Microsoft changes their login page's field names/IDs, the script will
  time out with an error rather than silently failing - run without
  `RDP_AAD_HEADLESS=1` to watch what's happening.
- 1Password's `op item get` reports a vault's *display name* (e.g.
  `Private`), which can differ from the name `op vault list` shows for the
  same vault ID (e.g. `Employee`). The script sidesteps this by always
  using vault/item IDs when building `op://` references, never names.
- Once the AAD login handshake is done, the script waits indefinitely for
  `xfreerdp` to exit on its own rather than applying any timeout - an idle
  RDP session can go hours without producing terminal output, and that's
  not a hang.
