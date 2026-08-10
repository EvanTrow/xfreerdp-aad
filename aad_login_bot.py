#!/usr/bin/env python3
"""
Drives xfreerdp through an Azure AD (Entra ID) login without human interaction.

xfreerdp's /sec:aad flow prints an OAuth2 "Browse to: <url>" line and then
blocks on stdin waiting for "Paste redirect URL here:". This script spawns
xfreerdp under a pty (via pexpect), watches for those two prompts, drives a
Playwright browser through the actual Microsoft login (username/password/TOTP
pulled live from the 1Password CLI), and feeds the resulting redirect URL
back to xfreerdp so the connection continues.

Usage: aad_login_bot.py <xfreerdp args...>
(Normally invoked via rdp-connect.sh, not directly.)
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import pexpect
from playwright.sync_api import sync_playwright

OP_ITEM = os.environ.get("RDP_AAD_OP_ITEM")
HEADLESS = os.environ.get("RDP_AAD_HEADLESS", "0") == "1"
DESKTOP = os.environ.get("RDP_AAD_DESKTOP", "").strip()
WM_CLASS = os.environ.get("RDP_AAD_WM_CLASS", "").strip()
LOGIN_TIMEOUT_MS = 60_000
DESKTOP_MOVE_TIMEOUT_S = 120

CERT_PROMPT = r"Do you trust the above certificate\? \(Y/T/N\)"
BROWSE_PROMPT = r"Browse to:\s*(\S+)"
PASTE_PROMPT = r"Paste redirect URL here:"


def op_json(item):
    result = subprocess.run(
        ["op", "item", "get", item, "--format", "json", "--reveal"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"1Password CLI failed for item {item!r}: {result.stderr.strip()}\n"
            "Is the 1Password CLI integration enabled in the desktop app "
            "(Settings > Developer), or do you need `eval $(op signin)`?"
        )
    return json.loads(result.stdout)


def op_field(data, purpose):
    for field in data.get("fields", []):
        if field.get("purpose") == purpose:
            return field.get("value", "")
    raise RuntimeError(f"1Password item {OP_ITEM!r} has no field with purpose {purpose}")


def op_otp(item, vault):
    # Target the "one-time password" field explicitly by label rather than
    # `op item get --otp`, which just picks a default when an item has
    # multiple OTP fields (this one does - it's shared with other logins).
    # item/vault are IDs here, which op:// references accept just like names.
    ref = f"op://{vault}/{item}/one-time password?attribute=otp"
    result = subprocess.run(["op", "read", ref], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"1Password CLI failed to read {ref!r}: {result.stderr.strip()}\n"
            "Does the item have a field labeled exactly 'one-time password'?"
        )
    return result.stdout.strip()


def get_credentials(item):
    data = op_json(item)
    # Use the vault ID, not its display name - 1Password reports a
    # human-readable alias ("Private") that doesn't match `op vault list`'s
    # actual name ("Employee") for the same vault ID, which breaks op://
    # references built from the name.
    vault = data["vault"]["id"]
    return op_field(data, "USERNAME"), op_field(data, "PASSWORD"), vault


def move_window_to_desktop(wm_class, desktop, timeout=DESKTOP_MOVE_TIMEOUT_S):
    """Poll for the xfreerdp window and move it to the given virtual
    desktop/workspace, then switch to it - mirrors `wmctrl -s <N>`.

    Runs in a background thread: the RDP window won't exist until AAD login
    and the RDP connection itself finish, which can take a while (browser
    MFA, etc.), so this shouldn't block the main login flow.
    """
    if not shutil.which("wmctrl"):
        print("[rdp-aad] wmctrl not found - can't move the RDP window", file=sys.stderr)
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        # -x -r matches WIN against WM_CLASS (substring, case-insensitive)
        # rather than window title, and moves it straight to -t's desktop.
        # Exit status is 0 only if a matching window was found and moved.
        result = subprocess.run(
            ["wmctrl", "-x", "-r", wm_class, "-t", str(desktop)], capture_output=True
        )
        if result.returncode == 0:
            subprocess.run(["wmctrl", "-s", str(desktop)])
            print(f"[rdp-aad] Moved RDP window to desktop {desktop}", file=sys.stderr)
            return
        time.sleep(1)

    print(
        f"[rdp-aad] Timed out waiting for the RDP window (wm-class {wm_class!r}) "
        f"to move it to desktop {desktop}",
        file=sys.stderr,
    )


def redirect_prefix_for(authorize_url):
    qs = parse_qs(urlparse(authorize_url).query)
    redirect_uri = qs.get("redirect_uri", [None])[0]
    if not redirect_uri:
        raise RuntimeError(f"Could not find redirect_uri in authorize URL: {authorize_url}")
    return redirect_uri


def do_browser_login(authorize_url, username, password, item, vault):
    prefix = redirect_prefix_for(authorize_url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()
        page.goto(authorize_url)

        page.locator('input[name="loginfmt"]').fill(username)
        page.locator("#idSIButton9").click()

        page.locator('input[name="passwd"]').wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
        page.locator('input[name="passwd"]').fill(password)
        page.locator("#idSIButton9").click()

        # Some tenants default to a push notification. If we don't see the
        # code field shortly, look for the "use a verification code instead"
        # option and switch to it.
        otc = page.locator('input[name="otc"]')
        try:
            otc.wait_for(state="visible", timeout=15_000)
        except Exception:
            alt = page.get_by_text(re.compile("verification code", re.I))
            if alt.count() > 0:
                alt.first.click()
                otc.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
            else:
                raise RuntimeError(
                    "Expected a one-time-code field but didn't see one, and no "
                    "'use a verification code' fallback link was found. "
                    "Check the visible browser window."
                )

        otc.fill(op_otp(item, vault))
        page.locator("#idSubmit_SAOTCC_Continue").click()

        # Drain "Stay signed in?" and any consent/permissions screen (e.g.
        # "Permissions requested" for the RDP client's device scope) if they
        # show up, then wait for the OAuth native-client redirect (it will
        # fail to load - that's expected - we only need the URL it tried to
        # navigate to).
        #
        # Microsoft reuses #idSIButton9 as the primary/forward action
        # (Next, Sign in, Accept) and #idBtn_Back as the secondary action
        # (Cancel, No, Back) across all these screens. The only screen where
        # the secondary action is what we *want* is "Stay signed in?" - on
        # a consent screen, clicking it means "deny access" and produces
        # exactly the access_denied/cancel error this is guarding against.
        deadline = time.time() + LOGIN_TIMEOUT_MS / 1000
        redirect_url = None
        while time.time() < deadline:
            if page.url.startswith(prefix):
                redirect_url = page.url
                break
            if page.get_by_text(re.compile("stay signed in", re.I)).count() > 0:
                no_btn = page.locator("#idBtn_Back")
                if no_btn.count() > 0 and no_btn.first.is_visible():
                    no_btn.first.click()
            else:
                accept_btn = page.locator("#idSIButton9")
                if accept_btn.count() > 0 and accept_btn.first.is_visible():
                    accept_btn.first.click()
            time.sleep(0.5)

        browser.close()

        if not redirect_url:
            raise RuntimeError(
                f"Timed out waiting for redirect to {prefix} - login likely "
                "did not complete (wrong credentials, extra Conditional "
                "Access prompt, etc.)."
            )

        error = parse_qs(urlparse(redirect_url).query).get("error")
        if error:
            desc = parse_qs(urlparse(redirect_url).query).get(
                "error_description", ["(no description)"]
            )[0]
            raise RuntimeError(f"AAD login returned error={error[0]!r}: {desc}")

        return redirect_url


def main():
    xfreerdp_args = sys.argv[1:]
    if not xfreerdp_args:
        print("Usage: aad_login_bot.py <xfreerdp args...>", file=sys.stderr)
        sys.exit(1)
    if not OP_ITEM:
        print(
            "RDP_AAD_OP_ITEM is not set - pass the 1Password item UUID "
            "(e.g. via rdp-connect.sh's second argument).",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = ["xfreerdp"] + xfreerdp_args
    print(f"+ {' '.join(shlex.quote(c) for c in cmd)}", file=sys.stderr)

    child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", timeout=120)
    child.setwinsize(50, 500)

    if DESKTOP:
        if not WM_CLASS:
            print(
                "[rdp-aad] RDP_AAD_DESKTOP is set but RDP_AAD_WM_CLASS is not - "
                "can't find the window to move, skipping",
                file=sys.stderr,
            )
        else:
            threading.Thread(
                target=move_window_to_desktop, args=(WM_CLASS, DESKTOP), daemon=True
            ).start()
    child.logfile_read = sys.stdout

    redirect_url = None
    exit_code = 0
    handed_off = False
    try:
        # Phase 1: drive the login handshake. A 120s timeout here is fine -
        # these prompts show up early, right after the TLS handshake.
        while True:
            idx = child.expect(
                [CERT_PROMPT, BROWSE_PROMPT, PASTE_PROMPT, pexpect.EOF, pexpect.TIMEOUT]
            )
            if idx == 0:
                # /cert:tofu should make this unreachable in normal operation.
                # Don't auto-trust a cert we weren't expecting - hand off to
                # a human instead of silently answering Y.
                child.logfile_read = None
                print(
                    "\n[rdp-aad] Unexpected certificate prompt - review it "
                    "yourself and answer Y/T/N:",
                    file=sys.stderr,
                )
                child.interact()
                handed_off = True
                break
            elif idx == 1:
                url = child.match.group(1)
                username, password, vault = get_credentials(OP_ITEM)
                print(
                    f"\n[rdp-aad] Got AAD login URL, signing in as {username} via browser...",
                    file=sys.stderr,
                )
                redirect_url = do_browser_login(url, username, password, OP_ITEM, vault)
                continue
            elif idx == 2:
                if not redirect_url:
                    raise RuntimeError("Hit the paste-redirect-URL prompt with no URL captured")
                child.sendline(redirect_url)
                print("[rdp-aad] Submitted redirect URL, connection in progress...", file=sys.stderr)
                break
            elif idx == 3:
                break
            elif idx == 4:
                print("[rdp-aad] Timed out waiting for the AAD login prompts", file=sys.stderr)
                exit_code = 1
                break

        # Phase 2: the login handshake is done (or wasn't needed). The RDP
        # session itself can sit idle for hours with zero terminal output,
        # so wait for xfreerdp to exit on its own instead of treating
        # silence as a hang.
        if not handed_off and child.isalive():
            child.expect(pexpect.EOF, timeout=None)
    except KeyboardInterrupt:
        print("\n[rdp-aad] Interrupted, terminating xfreerdp...", file=sys.stderr)
        child.kill(signal.SIGTERM)
    except Exception as exc:
        print(f"\n[rdp-aad] Error: {exc}", file=sys.stderr)
        child.kill(signal.SIGTERM)
        exit_code = 1

    child.close()
    sys.exit(exit_code if exit_code else (child.exitstatus or 0))


if __name__ == "__main__":
    main()
