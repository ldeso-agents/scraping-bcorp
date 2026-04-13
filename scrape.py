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

FIELDNAMES = [
    "date_added",
    "company_name",
    "company_website",
    "hq_country",
    "hq_city",
    "industry",
    "sector",
]


def collect_company_urls(page):
    """Load the directory and paginate to collect all company URLs and names.

    Returns a dict mapping each company's URL path to its name as shown on
    the directory card (extracted from the link's aria-label attribute).
    """
    page.goto(BASE_URL, wait_until="commit", timeout=60000)
    page.wait_for_selector(COMPANY_LINK_SELECTOR, timeout=120000)

    directory = {}
    current_page = 1

    while current_page <= 500:
        entries = page.eval_on_selector_all(
            COMPANY_LINK_SELECTOR,
            """els => [...new Map(els.map(a => {
                const href = a.getAttribute("href");
                const label = a.getAttribute("aria-label") || "";
                const name = label.replace(/^Link to /, "").replace(/ profile page$/, "");
                return [href, name];
            })).entries()]""",
        )
        for path, name in entries:
            if path not in directory:
                directory[path] = name
        print(
            f"  page {current_page}: {len(directory)} companies collected",
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
        old_first = entries[0][0] if entries else None

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

    return directory


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


def load_existing_csv(filepath):
    """Load existing CSV and return a dict mapping company_name to its row."""
    rows = {}
    try:
        with open(filepath, newline="") as f:
            for row in csv.DictReader(f):
                rows[row["company_name"]] = row
    except FileNotFoundError:
        pass
    return rows


def main():
    today = datetime.date.today().isoformat()

    # Load existing data so we can skip companies we already have.
    existing = load_existing_csv(OUTPUT_FILE)

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

        # Phase 1: Collect every company URL and card-name from the directory.
        print("Loading directory page...", flush=True)
        directory = collect_company_urls(page)
        print(f"Found {len(directory)} certified B Corps", flush=True)

        if not directory:
            print(
                "ERROR: No companies found on the directory page.",
                file=sys.stderr,
                flush=True,
            )
            browser.close()
            sys.exit(1)

        # Phase 2: Determine which companies are new.
        directory_names = set(directory.values())
        new_entries = [
            (path, name)
            for path, name in sorted(directory.items())
            if name not in existing
        ]
        print(f"  {len(new_entries)} new companies to scrape", flush=True)

        # Phase 3: Scrape detail pages only for new companies.
        for i, (path, name) in enumerate(new_entries, 1):
            slug = path.strip("/").rsplit("/", 1)[-1]
            print(f"[{i}/{len(new_entries)}] {slug}", flush=True)
            try:
                data = scrape_company_page(page, path)
                if data["company_name"]:
                    existing[data["company_name"]] = {
                        "date_added": today,
                        **data,
                    }
            except PlaywrightTimeoutError:
                print("  Timeout — skipping", flush=True)
            except Exception as e:
                print(f"  Error: {e}", flush=True)

        browser.close()

    # Phase 4: Write the CSV with only companies still in the directory.
    companies = [
        existing[name] for name in sorted(directory_names) if name in existing
    ]
    companies.sort(key=lambda c: c["company_name"].lower())

    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(companies)

    print(f"\nSaved {len(companies)} companies to {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
