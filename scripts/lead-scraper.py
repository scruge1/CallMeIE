"""
Lead scraper — finds local businesses from Google Maps that could use an AI receptionist.

Usage:
    python lead-scraper.py --industry "dentist" --city "Manchester" --limit 50
    python lead-scraper.py --industry "plumber" --city "London" --limit 30

Outputs CSV with: business_name, phone, address, website, rating, review_count
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("pip install playwright && playwright install chromium")
    exit(1)


def scrape_google_maps(industry: str, city: str, limit: int = 50) -> list[dict]:
    """Scrape business listings from Google Maps search results."""
    query = f"{industry} in {city}"
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Search Google Maps
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        page.goto(url, timeout=30000)
        time.sleep(5)

        # Accept cookies if prompted
        try:
            accept = page.query_selector('button:has-text("Accept all")')
            if accept:
                accept.click()
                time.sleep(2)
        except Exception:
            pass

        # Scroll results panel to load more
        results_panel = page.query_selector('[role="feed"]')
        if results_panel:
            for _ in range(min(limit // 5, 20)):
                results_panel.evaluate('el => el.scrollTop = el.scrollHeight')
                time.sleep(1.5)

        # Extract business data
        listings = page.query_selector_all('[data-result-index]')
        if not listings:
            listings = page.query_selector_all('.Nv2PK')

        for listing in listings[:limit]:
            try:
                name_el = listing.query_selector('.qBF1Pd, .fontHeadlineSmall')
                name = name_el.inner_text() if name_el else "Unknown"

                rating_el = listing.query_selector('.MW4etd')
                rating = rating_el.inner_text() if rating_el else "N/A"

                review_el = listing.query_selector('.UY7F9')
                reviews = review_el.inner_text().strip("()") if review_el else "0"

                # Click to get details
                listing.click()
                time.sleep(2)

                phone_el = page.query_selector(
                    'button[data-item-id*="phone"] .Io6YTe, '
                    '[data-tooltip="Copy phone number"] .Io6YTe'
                )
                phone = phone_el.inner_text() if phone_el else ""

                addr_el = page.query_selector(
                    'button[data-item-id*="address"] .Io6YTe'
                )
                address = addr_el.inner_text() if addr_el else ""

                web_el = page.query_selector(
                    'a[data-item-id*="authority"] .Io6YTe'
                )
                website = web_el.inner_text() if web_el else ""

                results.append(
                    {
                        "name": name,
                        "phone": phone,
                        "address": address,
                        "website": website,
                        "rating": rating,
                        "reviews": reviews,
                        "industry": industry,
                        "city": city,
                    }
                )

                # Go back to results
                page.go_back()
                time.sleep(1)

            except Exception as e:
                print(f"  Error scraping listing: {e}")
                continue

        browser.close()

    return results


def save_results(results: list[dict], output_dir: str = "."):
    """Save scraped leads to CSV."""
    if not results:
        print("No results to save")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    industry = results[0].get("industry", "unknown")
    city = results[0].get("city", "unknown")
    filename = f"leads_{industry}_{city}_{timestamp}.csv"
    filepath = os.path.join(output_dir, filename)

    fieldnames = [
        "name",
        "phone",
        "address",
        "website",
        "rating",
        "reviews",
        "industry",
        "city",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"Saved {len(results)} leads to {filepath}")
    return filepath


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape local business leads")
    parser.add_argument("--industry", required=True, help="e.g., dentist, plumber, salon")
    parser.add_argument("--city", required=True, help="e.g., Manchester, London")
    parser.add_argument("--limit", type=int, default=50, help="Max results")
    parser.add_argument("--output", default=".", help="Output directory")
    args = parser.parse_args()

    print(f"Scraping {args.industry} in {args.city} (limit: {args.limit})...")
    leads = scrape_google_maps(args.industry, args.city, args.limit)
    print(f"Found {len(leads)} businesses")
    save_results(leads, args.output)
