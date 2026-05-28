#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright>=1.44",
#   "playwright-stealth>=1.0.6",
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
from playwright.async_api import async_playwright, Page, BrowserContext, Cookie
from playwright_stealth import Stealth  # type: ignore[import-untyped]

FLOW_URL = "https://labs.google/fx/vi/tools/flow"
ACCOUNTS_DIR = Path("accounts")
CONFIG_FILE = Path("config.json")
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"

# None → playwright uses its own bundled chromium (installed via `playwright install chromium`)
CHROMIUM: str | None = (
    shutil.which("chromium")
    or shutil.which("chromium-browser")
    or shutil.which("google-chrome")
    or shutil.which("google-chrome-stable")
    or shutil.which("/run/current-system/sw/bin/chromium")  # NixOS
    or None
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    "--window-size=1280,800",
    "--disable-extensions",
    "--lang=en-US",
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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


async def google_oauth(page: Page, email: str, password: str) -> bool:
    print(f"[*] [{email}] Entering email...")
    try:
        _ = await page.wait_for_selector("input#identifierId", timeout=15000)
        await page.fill("input#identifierId", email)
        await asyncio.sleep(0.5)
        await page.click("#identifierNext")
        # Wait for transition to password page
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1)
    except Exception as e:
        print(f"[-] [{email}] Email step failed: {e}")
        return False

    print(f"[*] [{email}] Entering password...")
    try:
        # wait_for_selector checks visibility by default; no :visible needed
        _ = await page.wait_for_selector('input[type="password"]', timeout=15000)
        await page.fill('input[type="password"]', password)
        await asyncio.sleep(0.5)
        await page.click("#passwordNext")
    except Exception as e:
        print(f"[-] [{email}] Password step failed: {e}")
        return False

    return True


async def extract_session_token(email: str, password: str) -> str | None:
    acc_dir = account_dir(email)
    profile_dir = acc_dir / "chrome-profile"
    saved_session = load_saved_session(acc_dir)

    async with Stealth().use_async(async_playwright()) as p:
        context: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=CHROMIUM,
            headless=True,
            args=LAUNCH_ARGS,
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
                    print(f"[*] [{email}] Not authenticated — clicking 'Create with Google Flow'...")

                btn_html = await sign_in_btn.evaluate("el => el.outerHTML")
                print(f"[*] [{email}] Button: {btn_html[:200]}")

                await sign_in_btn.scroll_into_view_if_needed()
                await asyncio.sleep(0.5)
                _ = await sign_in_btn.click()
                print(f"[*] [{email}] Clicked. Polling for OAuth navigation...")

                # Poll up to 20s for popup or same-page navigation to accounts.google.com
                oauth_page: Page | None = None
                for i in range(20):
                    await asyncio.sleep(1)
                    pages = context.pages
                    print(f"[*] [{email}]   [{i+1}s] pages={len(pages)} url={page.url[:80]}")
                    if len(pages) > 1:
                        oauth_page = pages[-1]
                        await oauth_page.wait_for_load_state("domcontentloaded")
                        print(f"[*] [{email}] OAuth popup: {oauth_page.url}")
                        break
                    if "accounts.google.com" in page.url:
                        oauth_page = page
                        print(f"[*] [{email}] OAuth same-page: {page.url}")
                        break

                if oauth_page is None:
                    try:
                        body = await page.inner_text("body")
                        print(f"[*] [{email}] Page body snippet: {body[:400]}")
                    except Exception:
                        pass
                    print(f"[-] [{email}] Did not reach Google OAuth after 20s. URL: {page.url}")
                    return None

                ok = await google_oauth(oauth_page, email, password)
                if not ok:
                    return None

                print(f"[*] [{email}] Waiting for OAuth to complete...")
                try:
                    await oauth_page.wait_for_url(
                        lambda url: "labs.google" in url and "accounts.google" not in url,
                        timeout=30000,
                    )
                except Exception as e:
                    err_str = str(e)
                    if "closed" in err_str.lower():
                        # Popup closed after auth — normal for NextAuth postMessage flow
                        print(f"[*] [{email}] OAuth popup closed (session should be set).")
                    else:
                        try:
                            body = await oauth_page.inner_text("body")
                            if any(k in body.lower() for k in ("2-step", "verify", "confirm", "phone", "authenticator")):
                                print(f"[!] [{email}] 2FA required — headless cannot proceed.")
                                print("    Disable 2FA or set headless=False to complete manually.")
                            else:
                                print(f"[-] [{email}] Redirect timed out. URL: {oauth_page.url}")
                        except Exception:
                            print(f"[-] [{email}] Redirect timed out.")
                        return None

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
