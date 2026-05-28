#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "patchright>=1.44",
#   "httpx>=0.27",
# ]
# ///

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import TypedDict, cast
import httpx
from patchright.async_api import async_playwright, Page, BrowserContext, Cookie

FLOW_URL = "https://labs.google/fx/vi/tools/flow"
ACCOUNTS_DIR = Path("accounts")
CONFIG_FILE = Path("config.json")
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"

# None → playwright uses its own bundled chromium (installed via `playwright install chromium`)
# Prefer open-source Chromium builds; skip google-chrome which has WARP/network sandbox issues
CHROMIUM: str | None = None  # always use patchright's own patched binary

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    "--window-size=1280,800",
    "--disable-extensions",
    "--lang=en-US",
    # Required for Chrome (not Chromium) in headless server environments
    "--no-zygote",
    "--disable-gpu",
    "--disable-software-rasterizer",
    # WARP uses MASQUE (HTTP/3); disable Chrome's QUIC to avoid tunnel conflicts
    "--disable-quic",
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class Account(TypedDict):
    email: str
    password: str


class Config(TypedDict):
    apiUrl: str
    connectionToken: str
    accounts: list[Account]


class SavedCookie(TypedDict, total=False):
    name: str
    value: str
    domain: str
    path: str
    expires: float
    httpOnly: bool
    secure: bool
    sameSite: str


def load_config() -> Config:
    if not CONFIG_FILE.exists():
        print(f"[-] {CONFIG_FILE} not found. Create it first.")
        sys.exit(1)
    raw: object = json.loads(CONFIG_FILE.read_text())
    if not isinstance(raw, dict):
        print("[-] config.json must be a JSON object")
        sys.exit(1)
    if not raw.get("apiUrl") or not raw.get("connectionToken"):
        print("[-] config.json missing apiUrl or connectionToken")
        sys.exit(1)
    if not raw.get("accounts"):
        print("[-] config.json has no accounts defined")
        sys.exit(1)
    for i, acc in enumerate(raw["accounts"]):
        if "email" not in acc or "password" not in acc:
            print(f"[-] config.json accounts[{i}] missing 'email' or 'password'")
            sys.exit(1)
    return cast(Config, cast(object, raw))


def account_dir(email: str) -> Path:
    d = ACCOUNTS_DIR / email
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_saved_session(acc_dir: Path) -> SavedCookie | None:
    cookie_file = acc_dir / "session_cookie.json"
    if cookie_file.exists():
        try:
            raw: object = json.loads(cookie_file.read_text())
            return cast(SavedCookie, raw)
        except Exception:
            return None
    return None


def save_session_cookie(acc_dir: Path, cookie: Cookie) -> None:
    _ = (acc_dir / "session_cookie.json").write_text(json.dumps(dict(cookie), indent=2))


async def screenshot(page: Page, name: str) -> None:
    import os
    os.makedirs("/tmp/flowscreenshots", exist_ok=True)
    path = f"/tmp/flowscreenshots/{name}.png"
    await page.screenshot(path=path, full_page=True)
    print(f"[*] Screenshot: {path}")


async def google_signin_direct(page: Page, email: str, password: str) -> bool:
    """Log in via accounts.google.com directly, bypassing OAuth client browser checks."""
    safe = email.split("@")[0]
    GOOGLE_SIGNIN = "https://accounts.google.com/signin/v2/identifier?flowName=GlifWebSignIn&flowEntry=ServiceLogin"

    print(f"[*] [{email}] Navigating to Google sign-in directly...")
    try:
        _ = await page.goto(GOOGLE_SIGNIN, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        await screenshot(page, f"{safe}_1_direct_signin")
    except Exception as e:
        print(f"[-] [{email}] Failed to load Google sign-in: {e}")
        return False

    for attempt in range(4):
        print(f"[*] [{email}] Entering email (attempt {attempt+1})...")
        try:
            _ = await page.wait_for_selector("input#identifierId", timeout=15000)
            await page.fill("input#identifierId", email)
            await asyncio.sleep(0.5)
            await page.click("#identifierNext")
            await asyncio.sleep(4)
            await screenshot(page, f"{safe}_{attempt+1}_after_email_next")
        except Exception as e:
            print(f"[-] [{email}] Email step failed: {e}")
            await screenshot(page, f"{safe}_err_email_{attempt+1}")
            return False

        # Check for "Try again" block page vs password field
        await asyncio.sleep(2)
        try_again_loc = page.locator(':text("Try again")')
        if await try_again_loc.count() == 0:
            break  # no block page — password field should be up

        print(f"[*] [{email}] Blocked — clicking 'Try again' (attempt {attempt+1}). URL: {page.url}")
        await screenshot(page, f"{safe}_blocked_{attempt+1}")
        await try_again_loc.first.click()
        await asyncio.sleep(3)
    else:
        print(f"[-] [{email}] Exhausted retries on 'Couldn't sign you in'.")
        return False

    print(f"[*] [{email}] Entering password...")
    try:
        pwd_sel = 'input[type="password"]:not([aria-hidden="true"])'
        _ = await page.wait_for_selector(pwd_sel, timeout=15000)
        await page.fill(pwd_sel, password)
        await asyncio.sleep(0.5)
        await page.click("#passwordNext")
        await asyncio.sleep(3)
        await screenshot(page, f"{safe}_3_after_password")
    except Exception as e:
        await screenshot(page, f"{safe}_err_password")
        print(f"[-] [{email}] Password step failed: {e}")
        return False

    return True


async def google_oauth(page: Page, email: str, password: str) -> bool:
    safe = email.split("@")[0]
    print(f"[*] [{email}] Entering email...")
    try:
        _ = await page.wait_for_selector("input#identifierId", timeout=15000)
        await screenshot(page, f"{safe}_oauth_1_email")
        await page.fill("input#identifierId", email)
        await asyncio.sleep(0.5)
        await page.click("#identifierNext")
        await asyncio.sleep(3)
        await screenshot(page, f"{safe}_oauth_2_after_next")
    except Exception as e:
        print(f"[-] [{email}] Email step failed: {e}")
        await screenshot(page, f"{safe}_err_email")
        return False

    print(f"[*] [{email}] Entering password...")
    try:
        pwd_sel = 'input[type="password"]:not([aria-hidden="true"])'
        _ = await page.wait_for_selector(pwd_sel, timeout=15000)
        await page.fill(pwd_sel, password)
        await asyncio.sleep(0.5)
        await page.click("#passwordNext")
        await asyncio.sleep(2)
        await screenshot(page, f"{safe}_oauth_3_after_password")
    except Exception as e:
        await screenshot(page, f"{safe}_err_password")
        print(f"[-] [{email}] Password step failed: {e}")
        return False

    return True


async def extract_session_token(email: str, password: str) -> str | None:
    acc_dir = account_dir(email)
    profile_dir = acc_dir / "chrome-profile"
    saved_session = load_saved_session(acc_dir)

    async with async_playwright() as p:
        context: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"],
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 800},
            user_agent=UA,
        )

        try:
            page = context.pages[0] if context.pages else await context.new_page()

            if saved_session:
                print(f"[*] [{email}] Restoring saved session cookie...")
                await context.add_cookies([saved_session])  # pyright: ignore[reportArgumentType]

            print(f"[*] [{email}] Opening {FLOW_URL}...")
            _ = await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)

            sign_in_btn = await page.query_selector('button:has(span:text("Create with Google Flow"))')
            if sign_in_btn:
                if saved_session:
                    print(f"[*] [{email}] Saved session expired — re-authenticating...")
                else:
                    print(f"[*] [{email}] Not authenticated — signing in to Google directly first...")

                # Sign in to Google directly to establish session, avoiding
                # the OAuth client's embedded-browser block on the redirect flow
                ok = await google_signin_direct(page, email, password)
                if not ok:
                    return None

                # Check if we're signed in to Google (look for myaccount or redirected away from sign-in)
                await asyncio.sleep(2)
                safe = email.split("@")[0]
                await screenshot(page, f"{safe}_4_after_direct_signin")
                print(f"[*] [{email}] Google sign-in done. URL: {page.url}")

                # Now visit labs.google — Google session should trigger NextAuth automatically
                print(f"[*] [{email}] Navigating to {FLOW_URL} with active Google session...")
                _ = await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(4)
                await screenshot(page, f"{safe}_5_flow_after_signin")

                # Click the sign-in button if it still appears (triggers OAuth with active session)
                sign_in_btn2 = await page.query_selector('button:has(span:text("Create with Google Flow"))')
                if sign_in_btn2:
                    print(f"[*] [{email}] Clicking 'Create with Google Flow' with active session...")
                    _ = await sign_in_btn2.click()
                    await asyncio.sleep(3)
                    await screenshot(page, f"{safe}_6_after_flow_click")

                    # With active Google session OAuth should complete without password prompt
                    # Poll for completion
                    oauth_page: Page | None = None
                    for i in range(15):
                        await asyncio.sleep(1)
                        pages = context.pages
                        if len(pages) > 1:
                            oauth_page = pages[-1]
                            await oauth_page.wait_for_load_state("domcontentloaded")
                            print(f"[*] [{email}] OAuth popup: {oauth_page.url}")
                            break
                        if "labs.google" in page.url and "accounts.google" not in page.url:
                            print(f"[*] [{email}] Already on labs.google — session established")
                            break
                    else:
                        print(f"[*] [{email}] Still waiting... URL: {page.url}")

                # Reload flow page to pick up the now-active Google session
                _ = await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            else:
                print(f"[*] [{email}] Session active (restored from saved cookie)")

            print(f"[*] [{email}] Waiting 5s for JS to execute...")
            await asyncio.sleep(5)

            all_cookies: list[Cookie] = await context.cookies(["https://labs.google", FLOW_URL])
            unique = list({(c.get("name", ""), c.get("domain", "")): c for c in all_cookies}.values())

            session_token: str | None = None
            session_cookie: Cookie | None = None
            for c in unique:
                if c.get("name") == SESSION_COOKIE_NAME:
                    session_token = c.get("value")
                    session_cookie = c
                    print(f"[+] [{email}] session-token found (len={len(session_token or '')})")
                    break

            if not session_token:
                names = [c.get("name", "") for c in unique]
                print(f"[-] [{email}] session-token not found. Cookies: {names}")

            if session_token and session_cookie:
                save_session_cookie(acc_dir, session_cookie)
                _ = (acc_dir / "session_token.txt").write_text(session_token)

            return session_token

        finally:
            await context.close()


def send_token(api_url: str, connection_token: str, session_token: str, email: str) -> bool:
    try:
        resp = httpx.post(
            api_url,
            json={"session_token": session_token},
            headers={"Authorization": f"Bearer {connection_token}"},
            timeout=15,
        )
        _ = resp.raise_for_status()
        body: object = resp.json()
        if not isinstance(body, dict):
            print(f"[-] [{email}] Unexpected response shape: {body}")
            return False
        action = body.get("action", "synced")
        msg = body.get("message", "")
        print(f"[+] [{email}] Token {action}. {msg}")
        return True
    except httpx.HTTPStatusError as e:
        print(f"[-] [{email}] Server error {e.response.status_code}: {e.response.text[:200]}")
    except json.JSONDecodeError:
        print(f"[-] [{email}] Server returned non-JSON")
    except Exception as e:
        print(f"[-] [{email}] Request failed: {e}")
    return False


async def process_account(cfg: Config, email: str, password: str) -> bool:
    token = await extract_session_token(email, password)
    if not token:
        return False
    return send_token(cfg["apiUrl"], cfg["connectionToken"], token, email)


async def run_all(cfg: Config, target_email: str | None = None) -> None:
    accounts: list[Account] = cfg["accounts"]
    if target_email:
        accounts = [a for a in accounts if a["email"] == target_email]
        if not accounts:
            print(f"[-] Account {target_email!r} not found in config.json")
            sys.exit(1)

    results: dict[str, bool] = {}
    for acc in accounts:
        email = acc["email"]
        password = acc["password"]
        print(f"\n{'='*50}")
        print(f"Processing: {email}")
        print(f"{'='*50}")
        ok = await process_account(cfg, email, password)
        results[email] = ok

    print(f"\n{'='*50}")
    print("Summary:")
    for email, ok in results.items():
        status = "OK" if ok else "FAILED"
        print(f"  [{status}] {email}")

    failed = [e for e, ok in results.items() if not ok]
    if failed:
        sys.exit(1)


def main() -> None:
    print("=== Flow2API Session Token Extractor ===\n")
    cfg = load_config()
    target = sys.argv[1] if len(sys.argv) >= 2 else None
    asyncio.run(run_all(cfg, target))


if __name__ == "__main__":
    main()
