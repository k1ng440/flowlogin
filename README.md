# flowlogin

Google OAuth automation that extracts `__Secure-next-auth.session-token` from [Google Flow](https://labs.google/fx/vi/tools/flow) and pushes it to a [Flow2API](https://github.com/TheSmallHanCat/flow2api) instance.

## How it works

1. Launches Chromium via [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) in a virtual display (Xvfb) to bypass Google's bot detection
2. Signs in to Google directly, then navigates to Flow and clicks "Create with Google Flow"
3. Extracts the NextAuth session token from cookies
4. POSTs it to your Flow2API endpoint
5. Saves the session cookie locally — subsequent runs restore it without re-logging in

Token expires every ~12 hours. Run on a 55-minute schedule to keep it fresh indefinitely.

## Requirements

- Linux with `xvfb-run` (`apt install xvfb`)
- [uv](https://github.com/astral-sh/uv) — installed automatically by `install.sh`
- Accounts with 2FA **disabled**

## Setup

```bash
cp config.example.json config.json
# Edit config.json with your accounts and Flow2API details
```

```json
{
  "apiUrl": "http://192.168.0.200:8000/api/plugin/update-token",
  "connectionToken": "your-connection-token-here",
  "accounts": [
    { "email": "user1@gmail.com", "password": "password1" }
  ]
}
```

## Usage

```bash
# All accounts
xvfb-run --auto-servernum uv run --script login.py

# Single account
xvfb-run --auto-servernum uv run --script login.py user@gmail.com
```

## Install as systemd service (Linux)

Copies files to `/opt/flowlogin`, installs uv and patchright Chromium, and enables a timer that runs every 55 minutes.

```bash
sudo bash install.sh
```

```bash
# Logs
journalctl -u flowlogin.service -f

# Status
systemctl list-timers flowlogin.timer

# Manual trigger
systemctl start flowlogin.service
```

## File structure

```
accounts/
  user@gmail.com/
    chrome-profile/      # persistent browser session
    session_cookie.json  # saved cookie for session restore
    session_token.txt    # last extracted token
config.json              # credentials (gitignored)
login.py                 # main script
install.sh               # systemd installer
```

## Notes

- Uses [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (drop-in Playwright replacement) to strip automation signals
- Runs headful via Xvfb — Google's headless detection is the main blocker, virtual display bypasses it
- Works behind [Cloudflare WARP](https://1.1.1.1/) — QUIC disabled to avoid tunnel conflicts
- Session cookies are session-scoped (`expires: -1`) and not persisted by Chromium across restarts — the script saves and restores them manually
