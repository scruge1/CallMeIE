"""
Record a demo video of the AI receptionist handling a call.

Uses Playwright to:
1. Navigate to the Vapi assistant page
2. Click "Talk" to start a browser-based test call
3. Record the entire interaction as a video
4. Save to demo/ directory

Usage:
    python record-demo.py

Requirements:
    pip install playwright
    playwright install chromium
"""

import os
import time
import sys
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("pip install playwright && playwright install chromium")
    sys.exit(1)

# Configuration
ASSISTANT_URL = "https://dashboard.vapi.ai/assistants/0b37deb5-2fc2-4e7b-81b1-e61e97103506"
DEMO_DIR = Path(os.path.expanduser("~/Desktop/ai-agency/demo"))
DEMO_DIR.mkdir(parents=True, exist_ok=True)

PROFILE_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "swarm", "wallet", "chrome_profile_vapi"
)


def record_demo():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    video_path = str(DEMO_DIR)

    print(f"[*] Recording demo to {video_path}")
    print(f"[*] Assistant URL: {ASSISTANT_URL}")
    print()

    with sync_playwright() as p:
        # Use persistent context to reuse login session
        os.makedirs(PROFILE_DIR, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 720},
            record_video_dir=video_path,
            record_video_size={"width": 1280, "height": 720},
            locale="en-US",
        )

        page = context.pages[0] if context.pages else context.new_page()

        # Navigate to assistant
        print("[*] Navigating to Vapi assistant...")
        page.goto(ASSISTANT_URL, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)

        # Check if logged in
        body_text = page.evaluate("() => document.body.innerText")
        if "Sign Up" in body_text or "Log in" in body_text or "Create your account" in body_text:
            print("[!] Not logged in to Vapi. Please log in manually in the browser window.")
            print("[!] The script will wait 60 seconds for you to log in...")
            time.sleep(60)
            page.goto(ASSISTANT_URL, timeout=30000)
            time.sleep(5)

        print("[*] Looking for Talk button...")

        # Find and click the Talk button
        talk_clicked = page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                const text = b.textContent.trim();
                if (text === 'Talk' && b.offsetParent !== null) {
                    b.click();
                    return 'clicked';
                }
            }
            return 'not_found';
        }""")

        if talk_clicked == "clicked":
            print("[*] Talk button clicked — conversation starting!")
            print("[*] The AI will greet you. Speak naturally.")
            print()
            print("=" * 50)
            print("  DEMO RECORDING IN PROGRESS")
            print("  Speak into your microphone to test the AI.")
            print("  The conversation will be recorded.")
            print()
            print("  Suggested test scenarios:")
            print("  1. 'Hi, I'd like to book a cleaning'")
            print("  2. 'What insurance do you accept?'")
            print("  3. 'I have a toothache, is this an emergency?'")
            print("  4. 'What are your hours?'")
            print()
            print("  Press Ctrl+C when done to stop recording.")
            print("=" * 50)

            try:
                # Keep recording until user stops
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n[*] Stopping recording...")

        else:
            print("[!] Could not find Talk button. Taking screenshot...")
            page.screenshot(path=str(DEMO_DIR / "debug_screenshot.png"))
            print(f"[!] Screenshot saved to {DEMO_DIR / 'debug_screenshot.png'}")

        # Close and save video
        video_file = page.video.path()
        context.close()

        # Rename to something meaningful
        final_name = DEMO_DIR / f"dental_receptionist_demo_{timestamp}.webm"
        if video_file and os.path.exists(video_file):
            os.rename(video_file, str(final_name))
            print(f"\n[+] Demo video saved: {final_name}")
            print(f"[+] File size: {os.path.getsize(final_name) / 1024:.0f} KB")
        else:
            print("[!] Video file not found — recording may have failed")


if __name__ == "__main__":
    record_demo()
