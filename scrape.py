#!/usr/bin/env python3

"""Scrape certified companies from the B Corp directory using Playwright."""

import csv
import datetime
import re
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://www.bcorporation.net/en-us/find-a-b-corp/"
OUTPUT_FILE = "companies.csv"

SOCIAL_MEDIA_DOMAINS = [
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
    "x.com",
    "threads.net",
]

COMPANY_LINK_SELECTOR = 'a[href*="/find-a-b-corp/company/"]'


def collect_company_urls(page):
    """Load the directory and paginate through all pages to collect company URLs."""
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(COMPANY_LINK_SELECTOR, timeout=30000)

    all_urls = set()
    current_page = 1

    while current_page <= 500:
        links = page.eval_on_selector_all(
            COMPANY_LINK_SELECTOR,
            'els => [...new Set(els.map(a => a.getAttribute("href")))]',
        )
        all_urls.update(links)

        next_page = current_page + 1

        # Try to click the next page number button directly.
        btn = page.query_selector(
            f'button:text-is("{next_page}"), a:text-is("{next_page}")'
        )
        if not btn:
            # The button isn't visible yet — advance the pagination range.
            arrow = page.query_selector(
                'button[aria-label="Next page"], a[aria-label="Next page"], '
                'button[aria-label="Next"], a[aria-label="Next"]'
            )
            if not arrow or arrow.is_disabled():
                break
            arrow.click()
            page.wait_for_timeout(1000)
            btn = page.query_selector(
                f'button:text-is("{next_page}"), a:text-is("{next_page}")'
            )
            if not btn:
                break

        # Snapshot current company links so we can detect when the page updates.
        old_links = page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="/find-a-b-corp/company/"]'))
.map(a => a.href).join(",")"""
        )

        btn.click()

        # Wait until the company links change, indicating the new page loaded.
        try:
            page.wait_for_function(
                """(old) => Array.from(document.querySelectorAll('a[href*="/find-a-b-corp/company/"]'))
.map(a => a.href).join(",") !== old""",
                arg=old_links,
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            break

        page.wait_for_timeout(500)
        current_page = next_page

    return sorted(all_urls)


def scrape_company_page(page, company_path):
    """Visit a company page and extract all company details."""
    url = (
        company_path
        if company_path.startswith("http")
        else f"https://www.bcorporation.net{company_path}"
    )
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_selector("h1", timeout=15000)

    company_name = page.eval_on_selector("h1", "el => el.innerText.trim()")

    # Get the page text content to extract structured fields via label/value
    # pairs.  The company profile renders fields like:
    #   Headquarters
    #   California, United States
    #   Industry
    #   Apparel
    content = page.evaluate(
        """() => {
        const main = document.querySelector('main') || document.body;
        return main.innerText;
    }"""
    )

    # Parse headquarters (e.g. "California, United States").
    hq_country = ""
    hq_city = ""
    hq_match = re.search(r"Headquarters?\s*\n\s*(.+?)(?:\n|$)", content)
    if hq_match:
        hq = hq_match.group(1).strip()
        parts = [p.strip() for p in hq.split(",")]
        if len(parts) >= 2:
            hq_country = parts[-1]
            hq_city = ", ".join(parts[:-1])
        else:
            hq_country = hq

    # Parse industry.
    industry = ""
    ind_match = re.search(r"Industry\s*\n\s*(.+?)(?:\n|$)", content)
    if ind_match:
        industry = ind_match.group(1).strip()

    # Parse sector.
    sector = ""
    sec_match = re.search(r"Sector\s*\n\s*(.+?)(?:\n|$)", content)
    if sec_match:
        sector = sec_match.group(1).strip()

    # Extract the company website.  The link text usually looks like a bare
    # domain (e.g. "patagonia.com"), excluding B Corp and social-media URLs.
    company_website = ""
    links = page.eval_on_selector_all(
        "a[href^='http']",
        "els => els.map(a => ({href: a.getAttribute('href'), text: a.innerText.trim()}))",
    )
    for link in links:
        href = link["href"] or ""
        text = link["text"]
        if "bcorporation.net" in href or "blab." in href:
            continue
        if any(domain in href for domain in SOCIAL_MEDIA_DOMAINS):
            continue
        if text and "." in text and " " not in text:
            company_website = href
            break

    return {
        "company_name": company_name,
        "company_website": company_website,
        "hq_country": hq_country,
        "hq_city": hq_city,
        "industry": industry,
        "sector": sector,
    }


def load_existing_dates(filepath):
    """Load existing CSV to preserve original date_added values."""
    dates = {}
    try:
        with open(filepath, newline="") as f:
            for row in csv.DictReader(f):
                dates[row["company_name"]] = row["date_added"]
    except FileNotFoundError:
        pass
    return dates


def main():
    today = datetime.date.today().isoformat()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print("Loading directory page...")
        company_urls = collect_company_urls(page)
        print(f"Found {len(company_urls)} certified B Corps")

        if not company_urls:
            print("ERROR: No companies found on the directory page.", file=sys.stderr)
            browser.close()
            sys.exit(1)

        companies = []
        for i, company_path in enumerate(company_urls, 1):
            slug = company_path.rsplit("/", 1)[-1]
            print(f"[{i}/{len(company_urls)}] {slug}")
            try:
                data = scrape_company_page(page, company_path)
                if data["company_name"]:
                    companies.append({"date_added": today, **data})
            except PlaywrightTimeoutError:
                print("  Timeout — skipping")
            except Exception as e:
                print(f"  Error: {e}")

        browser.close()

    companies.sort(key=lambda c: c["company_name"].lower())

    # Preserve the original date_added for companies already in the CSV so the
    # field reflects when the company was *first* recorded, not the last time
    # the scraper ran.
    existing_dates = load_existing_dates(OUTPUT_FILE)
    for company in companies:
        original = existing_dates.get(company["company_name"])
        if original:
            company["date_added"] = original

    fieldnames = [
        "date_added",
        "company_name",
        "company_website",
        "hq_country",
        "hq_city",
        "industry",
        "sector",
    ]

    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(companies)

    print(f"\nSaved {len(companies)} companies to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
