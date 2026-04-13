#!/usr/bin/env python3

"""Scrape certified companies from the B Corp directory using Playwright."""

import csv
import datetime
import os
import re
import sys
from urllib.parse import urlparse

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
    page.goto(BASE_URL, wait_until="commit", timeout=60000)
    page.wait_for_selector(COMPANY_LINK_SELECTOR, timeout=120000)

    all_urls = set()
    current_page = 1

    while current_page <= 500:
        links = page.eval_on_selector_all(
            COMPANY_LINK_SELECTOR,
            'els => [...new Set(els.map(a => a.getAttribute("href")))]',
        )
        all_urls.update(links)
        print(
            f"  page {current_page}: {len(all_urls)} companies collected",
            flush=True,
        )

        next_page = current_page + 1

        # Try to click the next page number button directly.
        btn = page.query_selector(f'button[aria-label="Go to page {next_page}"]')
        if not btn:
            # The button isn't visible yet — advance the pagination range.
            arrow = page.query_selector('button[aria-label="Next"]')
            if not arrow or arrow.is_disabled():
                break
            arrow.click()
            page.wait_for_timeout(1000)
            btn = page.query_selector(
                f'button[aria-label="Go to page {next_page}"]'
            )
            if not btn:
                break

        # Snapshot the first company link so we can detect when the page updates.
        old_first = links[0] if links else None

        btn.click()

        # Wait until the company links change, indicating the new page loaded.
        try:
            page.wait_for_function(
                """(oldFirst) => {
                    const links = document.querySelectorAll('a[href*="/find-a-b-corp/company/"]');
                    return links.length > 0 && links[0].getAttribute("href") !== oldFirst;
                }""",
                arg=old_first,
                timeout=15000,
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
    page.goto(url, wait_until="commit", timeout=30000)
    page.wait_for_selector("h1", timeout=30000)

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
        launch_options = {"headless": True}

        # Forward the system proxy to Chromium when one is configured.
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy_url:
            parsed = urlparse(proxy_url)
            proxy_cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_cfg["username"] = parsed.username
            if parsed.password:
                proxy_cfg["password"] = parsed.password
            launch_options["proxy"] = proxy_cfg
            launch_options.setdefault("args", []).append(
                "--ignore-certificate-errors"
            )

        browser = p.chromium.launch(**launch_options)
        page = browser.new_page()

        print("Loading directory page...", flush=True)
        company_urls = collect_company_urls(page)
        print(f"Found {len(company_urls)} certified B Corps", flush=True)

        if not company_urls:
            print("ERROR: No companies found on the directory page.", file=sys.stderr, flush=True)
            browser.close()
            sys.exit(1)

        companies = []
        for i, company_path in enumerate(company_urls, 1):
            slug = company_path.strip("/").rsplit("/", 1)[-1]
            print(f"[{i}/{len(company_urls)}] {slug}", flush=True)
            try:
                data = scrape_company_page(page, company_path)
                if data["company_name"]:
                    companies.append({"date_added": today, **data})
            except PlaywrightTimeoutError:
                print("  Timeout — skipping", flush=True)
            except Exception as e:
                print(f"  Error: {e}", flush=True)

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

    print(f"\nSaved {len(companies)} companies to {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
