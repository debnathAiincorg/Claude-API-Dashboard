"""
fetch_billing.py
----------------
Reads ONLY the "Credit balance" amount from
https://platform.claude.com/settings/billing and appends it (with a timestamp)
to billing_data.json. No server is used:

  1. this script fetches the balance and saves it in billing_data.json
     (plus billing_data.js - the same data, in a form a double-clicked HTML
     file is allowed to load; browsers block reading .json from file://)
  2. billing_dashboard.html (just double-click it) re-loads that data every
     few seconds and redraws the chart when it changes.

Uses Microsoft Edge with its OWN separate profile. You log in there once with
the account that owns the API credits; your normal Edge is not touched and can
stay open.

(Copying the login from your normal Edge profile does not work: Edge encrypts
its cookies with a key tied to its own folder, so a copied login can't be read.)

Run (the only command):
    python fetch_billing.py

It fetches the balance once, saves it, uploads it to GitHub, and ends.
GitHub then publishes the dashboard (.github/workflows/fetch-balance.yml).
If you are not signed in, it opens a sign-in window by itself.

The login is stored in ".edge_billing_profile" next to this script.
Delete that folder to log out / switch account. Don't share it.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BILLING_URL = "https://platform.claude.com/settings/billing"
HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "billing_dashboard.html"
DATA_FILE = HERE / "billing_data.json"
DATA_JS_FILE = HERE / "billing_data.js"
PROFILE_DIR = HERE / ".edge_billing_profile"

BALANCE_LINE_RE = re.compile(r"-?\$\s*([\d,]+(?:\.\d{1,2})?)")
# Text shown on the sign-in screen (it is served at the billing URL when logged out).
LOGIN_MARKERS = ("continue with email", "continue with google")
FETCH_WAIT_SECONDS = 45


class NotLoggedIn(Exception):
    pass


def extract_balance(page_text: str):
    """Return the $ amount shown inside the 'Credit balance' box.

    Only the text between the 'Credit balance' heading and the box's
    'Buy credits' button is searched, and the amount must be on its own line.
    While the balance still says 'Loading', this returns None - it must never
    pick up other amounts further down the page (e.g. invoice history).
    """
    start = page_text.lower().find("credit balance")
    if start == -1:
        return None
    amount = None
    for line in page_text[start:].splitlines():
        line = line.strip()
        if line.lower() == "buy credits":  # the button = end of the balance box
            return amount
        match = BALANCE_LINE_RE.fullmatch(line)
        if match and amount is None:
            amount = float(match.group(1).replace(",", ""))
    return None  # box not fully rendered yet


def is_login_screen(page_text: str) -> bool:
    low = page_text.lower()
    return "credit balance" not in low and any(m in low for m in LOGIN_MARKERS)


def open_edge(p, headless: bool):
    try:
        return p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="msedge",
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
    except Exception as e:
        if "exitCode=21" in str(e) or "has been closed" in str(e):
            sys.exit("The script's Edge window (sign-in window) is still open.\n"
                     "Close that window first, then run the command again.")
        raise


def find_edge() -> str:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            exe = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if exe.exists():
                return str(exe)
    sys.exit("Microsoft Edge (msedge.exe) was not found.")


def login():
    """Open a NORMAL (not automated) Edge window on the script's own profile so
    the user can sign in once. Claude's sign-in refuses automated browsers, so
    Playwright is only used afterwards, to read the page."""
    PROFILE_DIR.mkdir(exist_ok=True)
    subprocess.Popen([find_edge(), f"--user-data-dir={PROFILE_DIR}",
                      "--no-first-run", "--no-default-browser-check", BILLING_URL])
    print("A separate Edge window is open (your normal Edge is not affected).\n"
          "1. Sign in with the account that holds the API credits.\n"
          "2. If that account has several organizations, switch to the right one.\n"
          "3. When the Billing page shows the Credit balance, CLOSE that Edge window.")
    input("Then press Enter here...")
    stop_profile_edge()


def profile_edge_pids():
    """PIDs of Edge processes using the script's own profile folder (never your normal Edge)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{PROFILE_DIR.name}*' }} | "
          "ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True).stdout
    return [pid for pid in out.split() if pid.isdigit()]


def stop_profile_edge():
    """Edge keeps running in the background after its window is closed. Wait a
    moment for it to exit by itself, then end it - only the script's profile."""
    for _ in range(10):
        if not profile_edge_pids():
            return
        time.sleep(1)
    for pid in profile_edge_pids():
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    time.sleep(2)


def fetch_balance(headless: bool = True) -> float:
    from playwright.sync_api import sync_playwright

    if not PROFILE_DIR.exists():
        raise NotLoggedIn("Not signed in yet.")
    stop_profile_edge()  # a leftover background Edge on our profile would block launch

    with sync_playwright() as p:
        ctx = open_edge(p, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BILLING_URL, wait_until="domcontentloaded")
        deadline = time.time() + FETCH_WAIT_SECONDS
        balance, logged_out = None, False
        while time.time() < deadline:
            try:
                if "/settings/billing" in page.url:
                    text = page.inner_text("body")
                    balance = extract_balance(text)
                    if balance is not None:
                        break
                    if is_login_screen(text):
                        logged_out = True
                        break
            except Exception:
                pass  # page still loading / redirecting
            page.wait_for_timeout(1500)
        ctx.close()

    if logged_out:
        raise NotLoggedIn("The sign-in has expired.")
    if balance is None:
        sys.exit("Could not find the Credit balance on the billing page - will try again next time.")
    return balance


def load_points() -> list:
    if not DATA_FILE.exists():
        return []
    raw = DATA_FILE.read_text(encoding="utf-8").strip()
    return json.loads(raw) if raw else []


def save_point(balance: float) -> int:
    points = load_points()
    points.append({
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "balance": round(balance, 2),
    })
    text = json.dumps(points, indent=1)
    write_file(DATA_FILE, text + "\n")
    write_file(DATA_JS_FILE, "// Written by fetch_billing.py - copy of billing_data.json for the dashboard.\n"
                             "window.BILLING_DATA = " + text + ";\n")
    return len(points)


def write_file(path: Path, text: str):
    """Write to a temp file, then swap it in - the dashboard never reads a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def record(balance: float):
    n = save_point(balance)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] Credit balance: ${balance:,.2f}  ({n} readings saved)", flush=True)


def main():
    """Fetch the balance once, save it, and end."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        sys.exit("Playwright is not installed. Run:  pip install playwright")

    try:
        record(fetch_balance())
    except NotLoggedIn as e:
        print(f"{e} Opening the sign-in window...", flush=True)
        login()
        try:
            record(fetch_balance())
        except NotLoggedIn:
            sys.exit("Still not signed in. Run the command again and sign in.")
    upload_to_github()


def git(*args):
    return subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True)


def upload_to_github():
    """Send the new reading to GitHub. GitHub then publishes the dashboard by itself."""
    if git("remote", "get-url", "origin").returncode != 0:
        return  # not connected to GitHub - the reading is still saved on this PC
    git("add", DATA_FILE.name, DATA_JS_FILE.name)
    git("commit", "-m", "New balance reading")
    push = git("push")
    if push.returncode != 0:  # GitHub has newer commits - take them first, then retry
        git("pull", "--rebase")
        push = git("push")
    if push.returncode == 0:
        print("Uploaded to GitHub - the online dashboard updates in a few minutes.")
    else:
        print("Saved on this PC, but uploading to GitHub failed:\n" + push.stderr.strip())


if __name__ == "__main__":
    main()
