"""
Google Search Console -- full automated verification flow.
Steps:
  1. Log into Google with swarm agent account
  2. Add callmeie.ie as a Domain property in GSC
  3. Grab the DNS TXT verification token
  4. Add TXT record to callmeie.ie via Porkbun API
  5. Click Verify in GSC
  6. Submit sitemap

Run from a terminal: python scripts/gsc_verify.py
"""
import sys
import time
import re
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --- Credentials ---
GOOGLE_EMAIL    = "swarm.agent.2026@gmail.com"
GOOGLE_PASSWORD = "Claude2026"
TARGET_DOMAIN   = "callmeie.ie"
SITEMAP_URL     = "https://callmeie.ie/sitemap.xml"

PORKBUN_API_KEY    = "pk1_4e7a4dd510c137d21e9ec8197c2fa4fff8d500d91fa65ae9a47984f3da5cc0c7"
PORKBUN_SECRET_KEY = "sk1_5c8932e9bb3f85427fd409c114a9d763ff622d3f68fa62fbd4ac5978a716c2ea"
PORKBUN_BASE       = "https://api.porkbun.com/api/json/v3"

# --------------------------------------------------------------------------

def say(msg):
    print(msg, flush=True)


def add_dns_txt(token: str) -> dict:
    payload = {
        "apikey": PORKBUN_API_KEY,
        "secretapikey": PORKBUN_SECRET_KEY,
        "name": "",
        "type": "TXT",
        "content": token,
        "ttl": "300",
    }
    r = requests.post(f"{PORKBUN_BASE}/dns/create/{TARGET_DOMAIN}", json=payload, timeout=15)
    return r.json()


def list_dns_txt() -> list:
    payload = {"apikey": PORKBUN_API_KEY, "secretapikey": PORKBUN_SECRET_KEY}
    r = requests.post(f"{PORKBUN_BASE}/dns/retrieveByNameType/{TARGET_DOMAIN}/TXT", json=payload, timeout=15)
    return r.json().get("records", [])


def wait_for_url_not_containing(page, fragment, timeout_s=120):
    """Poll until URL no longer contains the given fragment."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if fragment not in page.url:
            return True
        time.sleep(2)
    return False


def wait_for_url_containing(page, fragment, timeout_s=120):
    """Poll until URL contains the given fragment."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if fragment in page.url:
            return True
        time.sleep(2)
    return False


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        # ── 1. Google Login ──────────────────────────────────────────────────
        say("-> Navigating to Google Sign-In...")
        page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded")
        time.sleep(2)

        say("-> Entering email...")
        try:
            email_field = page.locator('input[type="email"]')
            email_field.wait_for(timeout=10000)
            email_field.fill(GOOGLE_EMAIL)
            page.keyboard.press("Enter")
        except Exception as e:
            say(f"  Email field issue: {e}")

        time.sleep(3)

        say("-> Entering password...")
        try:
            pwd_field = page.locator('input[type="password"]')
            pwd_field.wait_for(timeout=10000)
            pwd_field.fill(GOOGLE_PASSWORD)
            page.keyboard.press("Enter")
        except Exception as e:
            say(f"  Password field issue: {e}")

        time.sleep(3)

        # Handle Google security challenges (2FA, verify it's you, etc.)
        say(f"  Post-login URL: {page.url}")

        if "accounts.google.com" in page.url:
            say("")
            say("=" * 60)
            say("ACTION NEEDED: Google security challenge detected.")
            say("Complete the verification in the browser window.")
            say("Waiting up to 3 minutes for you to finish...")
            say("=" * 60)
            say("")
            # Poll until we leave accounts.google.com
            ok = wait_for_url_not_containing(page, "accounts.google.com", timeout_s=180)
            if not ok:
                page.screenshot(path="gsc_login_stuck.png")
                say("[!] Still on Google accounts page after 3min. Screenshot: gsc_login_stuck.png")
                say("    Please complete login in the browser, then the script will continue.")
                # Keep waiting another 2 minutes
                wait_for_url_not_containing(page, "accounts.google.com", timeout_s=120)

        say(f"  Logged in. URL: {page.url}")

        # ── 2. Search Console — add Domain property ──────────────────────────
        say("-> Opening Search Console...")
        page.goto(
            "https://search.google.com/search-console/welcome",
            wait_until="domcontentloaded",
        )
        time.sleep(4)

        # If redirected back to Google sign-in, wait for user to fix it
        if "accounts.google.com" in page.url:
            say("")
            say("=" * 60)
            say("ACTION NEEDED: Redirected to Google login on GSC navigate.")
            say("Please log in manually in the browser window.")
            say("Waiting up to 3 minutes...")
            say("=" * 60)
            wait_for_url_not_containing(page, "accounts.google.com", timeout_s=180)
            page.goto(
                "https://search.google.com/search-console/welcome",
                wait_until="domcontentloaded",
            )
            time.sleep(4)

        say(f"  GSC page title: {page.title()}")
        say(f"  GSC URL: {page.url}")
        page.screenshot(path="gsc_welcome.png")
        say("  Screenshot: gsc_welcome.png")

        # ── 3. Fill domain input ─────────────────────────────────────────────
        say("-> Looking for Domain property input...")
        domain_filled = False

        # Try several selectors the GSC welcome page might use
        selectors = [
            'input[placeholder*="example.com"]',
            'input[placeholder*="domain"]',
            'input[aria-label*="omain"]',
            'input[aria-label*="Domain"]',
            # The left panel "Domain" radio + input
            'input[type="text"]',
        ]

        for sel in selectors:
            try:
                el = page.locator(sel).first
                el.wait_for(timeout=3000)
                # Only fill if the placeholder/label looks right, or if it's a plain text input
                say(f"  Found input with selector: {sel}")
                el.fill(TARGET_DOMAIN)
                domain_filled = True
                break
            except Exception:
                continue

        if not domain_filled:
            # GSC may show a different UI if properties already exist.
            # Try clicking "Add property" button first.
            say("  Domain input not found directly — trying 'Add property' button...")
            try:
                add_btn = page.locator('text="Add property", text="Add a property"').first
                add_btn.wait_for(timeout=5000)
                add_btn.click()
                time.sleep(2)
                # Now try inputs again
                for sel in selectors:
                    try:
                        el = page.locator(sel).first
                        el.wait_for(timeout=3000)
                        el.fill(TARGET_DOMAIN)
                        domain_filled = True
                        break
                    except Exception:
                        continue
            except Exception as e:
                say(f"  Add property button not found: {e}")

        if not domain_filled:
            page.screenshot(path="gsc_debug2.png")
            say("")
            say("=" * 60)
            say("ACTION NEEDED: Could not auto-fill domain input.")
            say("Screenshot saved: gsc_debug2.png")
            say("Please manually:")
            say("  1. Select 'Domain' tab in GSC")
            say("  2. Type: callmeie.ie")
            say("  3. Click Continue/Submit")
            say("Waiting 3 minutes for you to do this...")
            say("=" * 60)
            time.sleep(180)
        else:
            say("-> Submitting domain...")
            page.keyboard.press("Enter")
            time.sleep(4)

        # ── 4. Grab TXT verification token ───────────────────────────────────
        say("-> Looking for TXT verification token in page...")
        time.sleep(3)

        txt_token = None
        for attempt in range(10):
            content = page.content()
            match = re.search(r"google-site-verification=[A-Za-z0-9_\-]+", content)
            if match:
                txt_token = match.group(0)
                say(f"[OK] TXT token: {txt_token}")
                break
            say(f"  Attempt {attempt+1}/10 — token not found yet, waiting 5s...")
            time.sleep(5)

        if not txt_token:
            page.screenshot(path="gsc_token_missing.png")
            say("")
            say("=" * 60)
            say("ACTION NEEDED: Could not auto-detect TXT token.")
            say("Screenshot: gsc_token_missing.png")
            say("Copy the 'google-site-verification=...' value from the GSC screen.")
            say("Then paste it below and press Enter:")
            say("=" * 60)
            # In interactive terminal this works; if stdin is closed it'll raise
            try:
                txt_token = sys.stdin.readline().strip()
                if not txt_token:
                    say("[!] No token entered. Exiting.")
                    browser.close()
                    return
            except Exception:
                say("[!] stdin not available. Cannot continue without token.")
                browser.close()
                return

        # ── 5. Add TXT record via Porkbun ────────────────────────────────────
        say(f"\n-> Adding TXT record to {TARGET_DOMAIN} via Porkbun API...")
        result = add_dns_txt(txt_token)
        say(f"  Porkbun response: {result}")
        if result.get("status") == "SUCCESS":
            say("[OK] TXT record created.")
        else:
            say(f"  Note: {result.get('message', result)}")

        time.sleep(2)
        records = list_dns_txt()
        matches = [r for r in records if txt_token in r.get("content", "")]
        say(f"  TXT records on {TARGET_DOMAIN}: {len(records)} total, token present: {bool(matches)}")

        # ── 6. Verify in GSC ─────────────────────────────────────────────────
        say("\n-> Waiting 20s for DNS propagation before clicking Verify...")
        time.sleep(20)

        for attempt in range(3):
            try:
                verify_btn = page.locator(
                    'button:has-text("Verify"), div[role="button"]:has-text("Verify")'
                ).first
                verify_btn.wait_for(timeout=6000)
                verify_btn.click()
                time.sleep(5)
                say("[OK] Verify clicked.")
                page.screenshot(path="gsc_verify_result.png")
                say("  Screenshot: gsc_verify_result.png")
                break
            except Exception as e:
                say(f"  Verify attempt {attempt+1} failed: {e}")
                page.reload()
                time.sleep(5)
        else:
            say("")
            say("=" * 60)
            say("ACTION NEEDED: Could not find Verify button automatically.")
            say("Please click 'Verify' in the browser window.")
            say("Waiting 2 minutes...")
            say("=" * 60)
            time.sleep(120)

        # ── 7. Submit Sitemap ─────────────────────────────────────────────────
        say(f"\n-> Navigating to sitemap submission page...")
        sitemap_page = (
            f"https://search.google.com/search-console/sitemaps"
            f"?resource_id=sc-domain%3A{TARGET_DOMAIN}"
        )
        page.goto(sitemap_page, wait_until="domcontentloaded")
        time.sleep(4)

        try:
            sitemap_input = page.locator(
                'input[placeholder*="itemap"], input[aria-label*="itemap"]'
            ).first
            sitemap_input.wait_for(timeout=8000)
            sitemap_input.fill("sitemap.xml")
            page.keyboard.press("Enter")
            time.sleep(3)
            try:
                submit_btn = page.locator('button:has-text("Submit")').first
                submit_btn.wait_for(timeout=5000)
                submit_btn.click()
                time.sleep(3)
                say("[OK] Sitemap submitted.")
            except PWTimeout:
                say("  Submit button not found — may have auto-submitted on Enter.")
        except PWTimeout:
            page.screenshot(path="gsc_sitemap.png")
            say("  Sitemap input not found. Screenshot: gsc_sitemap.png")
            say("  Please submit sitemap manually in the browser.")
            time.sleep(60)

        page.screenshot(path="gsc_final.png")
        say("\n[DONE] Final screenshot: gsc_final.png")
        say(f"  TXT token used: {txt_token}")
        say("\nBrowser will stay open for 60 seconds so you can review, then close.")
        time.sleep(60)
        browser.close()


if __name__ == "__main__":
    run()
