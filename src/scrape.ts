import { chromium, type BrowserContext, type Page } from "playwright";
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const BASE_URL = "https://www.bcorporation.net";
const DIRECTORY_URL = `${BASE_URL}/en-us/find-a-b-corp/`;
const OUTPUT_FILE = resolve(process.env.OUTPUT_FILE ?? "bcorp_companies.csv");
const CONCURRENCY = parseInt(process.env.CONCURRENCY ?? "3", 10);
const MAX_RETRIES = 3;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Company {
  date_added: string;
  company_name: string;
  company_website: string;
  hq_country: string;
  hq_city: string;
  industry: string;
  sector: string;
}

// ---------------------------------------------------------------------------
// CSV helpers
// ---------------------------------------------------------------------------

const CSV_HEADER =
  "date_added,company_name,company_website,hq_country,hq_city,industry,sector";

function escapeCSV(value: string): string {
  if (
    value.includes(",") ||
    value.includes('"') ||
    value.includes("\n") ||
    value.includes("\r")
  ) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

function companyToRow(c: Company): string {
  return [
    c.date_added,
    c.company_name,
    c.company_website,
    c.hq_country,
    c.hq_city,
    c.industry,
    c.sector,
  ]
    .map(escapeCSV)
    .join(",");
}

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function today(): string {
  return new Date().toISOString().split("T")[0];
}

/** Parse a headquarters string like "California, United States" into city and
 *  country components. The last comma-separated token is treated as the
 *  country; everything before it is the city / region. */
function parseHQ(raw: string): { city: string; country: string } {
  const parts = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (parts.length === 0) return { city: "", country: "" };
  if (parts.length === 1) return { city: "", country: parts[0] };
  return {
    country: parts[parts.length - 1],
    city: parts.slice(0, -1).join(", "),
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// ---------------------------------------------------------------------------
// CSV persistence (supports resuming interrupted runs)
// ---------------------------------------------------------------------------

/** Return the set of company names already present in the CSV so we can skip
 *  them on a resumed run. Creates the file with a header if it doesn't exist. */
function initCSV(): Set<string> {
  const existing = new Set<string>();
  if (existsSync(OUTPUT_FILE)) {
    const lines = readFileSync(OUTPUT_FILE, "utf-8").split("\n").slice(1);
    for (const line of lines) {
      if (!line.trim()) continue;
      // Extract the company_name field (second column). Handles both quoted
      // and unquoted values.
      const match = line.match(/^[^,]*,("(?:[^"]|"")*"|[^,]*)/);
      if (match) {
        existing.add(match[1].replace(/^"|"$/g, "").replace(/""/g, '"'));
      }
    }
    console.log(
      `Resuming — found ${existing.size} companies already in ${OUTPUT_FILE}`,
    );
  } else {
    writeFileSync(OUTPUT_FILE, CSV_HEADER + "\n");
    console.log(`Created ${OUTPUT_FILE}`);
  }
  return existing;
}

function appendCompany(company: Company): void {
  appendFileSync(OUTPUT_FILE, companyToRow(company) + "\n");
}

// ---------------------------------------------------------------------------
// Phase 1 — Collect every company detail-page URL from the directory listing
// ---------------------------------------------------------------------------

async function dismissOverlays(page: Page): Promise<void> {
  for (const text of [
    "Accept All",
    "Accept all",
    "Accept",
    "I agree",
    "OK",
    "Got it",
    "Close",
  ]) {
    const btn = page.locator(`button:has-text("${text}")`).first();
    if (await btn.isVisible({ timeout: 500 }).catch(() => false)) {
      await btn.click().catch(() => {});
      await sleep(500);
    }
  }
}

async function collectCompanyUrls(page: Page): Promise<string[]> {
  console.log("Navigating to directory page…");
  await page.goto(DIRECTORY_URL, {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  await page.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => {});
  await dismissOverlays(page);

  // The page may require an explicit search trigger (e.g. pressing Enter in
  // the search box) before it shows results.
  const hasResults = await page
    .waitForSelector('a[href*="/find-a-b-corp/company/"]', { timeout: 15_000 })
    .then(() => true)
    .catch(() => false);

  if (!hasResults) {
    console.log("No results visible yet — trying to trigger a search…");
    const input = page
      .locator(
        'input[type="search"], input[type="text"], input[placeholder*="earch" i]',
      )
      .first();
    if (await input.isVisible().catch(() => false)) {
      await input.press("Enter");
    }
    await page
      .waitForSelector('a[href*="/find-a-b-corp/company/"]', {
        timeout: 30_000,
      })
      .catch(() => {});
  }

  const allUrls = new Set<string>();
  let staleRounds = 0;

  // Paginate until we stop finding new company links.
  while (staleRounds < 2) {
    const urls: string[] = await page.$$eval(
      'a[href*="/find-a-b-corp/company/"]',
      (links) =>
        [...new Set(links.map((a) => a.getAttribute("href")).filter(Boolean))] as string[],
    );

    const sizeBefore = allUrls.size;
    for (const u of urls) {
      allUrls.add(u.startsWith("http") ? u : `${BASE_URL}${u}`);
    }
    console.log(
      `  collected ${urls.length} links on this view — ${allUrls.size} unique total`,
    );

    if (allUrls.size === sizeBefore) {
      staleRounds++;
    } else {
      staleRounds = 0;
    }

    // ----- Try every common pagination pattern -----

    let advanced = false;

    // 1) Explicit "Next" / ">" / "→" button
    for (const sel of [
      'button:has-text("Next")',
      'a:has-text("Next")',
      '[aria-label="Next"]',
      '[aria-label="Next page"]',
      'nav[aria-label="Pagination"] li:last-child a',
      'nav[aria-label="Pagination"] li:last-child button',
    ]) {
      const btn = page.locator(sel).first();
      if (
        (await btn.isVisible().catch(() => false)) &&
        (await btn.isEnabled().catch(() => false))
      ) {
        await btn.click();
        await page
          .waitForLoadState("networkidle", { timeout: 15_000 })
          .catch(() => {});
        await sleep(1_000);
        advanced = true;
        break;
      }
    }
    if (advanced) continue;

    // 2) "Load more" / "Show more" button
    for (const sel of [
      'button:has-text("Load more")',
      'button:has-text("Show more")',
      'button:has-text("View more")',
      'button:has-text("See more")',
      'a:has-text("Load more")',
    ]) {
      const btn = page.locator(sel).first();
      if (await btn.isVisible().catch(() => false)) {
        await btn.click();
        await sleep(3_000);
        await page
          .waitForLoadState("networkidle", { timeout: 15_000 })
          .catch(() => {});
        advanced = true;
        break;
      }
    }
    if (advanced) continue;

    // 3) Infinite-scroll: scroll to the bottom and wait for new content
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await sleep(3_000);
  }

  return [...allUrls];
}

// ---------------------------------------------------------------------------
// Phase 2 — Scrape individual company detail pages
// ---------------------------------------------------------------------------

async function scrapeCompanyPage(
  page: Page,
  url: string,
): Promise<Company | null> {
  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
      await page.waitForSelector("h1", { timeout: 15_000 });

      const data = await page.evaluate(() => {
        /** Walk the DOM to find a <span> whose text matches `label`, then
         *  return the text (or href) of the nearest value element. */
        function fieldValue(label: string): string {
          for (const span of document.querySelectorAll("span")) {
            if (span.textContent?.trim() !== label) continue;
            // Value is typically in the next sibling of the <span>, or
            // in the next sibling of the <span>'s parent.
            for (const candidate of [
              span.nextElementSibling,
              span.parentElement?.nextElementSibling,
            ]) {
              if (!candidate) continue;
              const link = candidate.querySelector("a");
              if (link) {
                return (
                  link.getAttribute("href") ||
                  link.textContent?.trim() ||
                  ""
                );
              }
              const text = candidate.textContent?.trim();
              if (text) return text;
            }
          }
          return "";
        }

        return {
          name: document.querySelector("h1")?.textContent?.trim() ?? "",
          website: fieldValue("Website"),
          headquarters: fieldValue("Headquarters"),
          industry: fieldValue("Industry"),
          sector: fieldValue("Sector"),
        };
      });

      if (!data.name) {
        // Might be a 404 page
        const is404 =
          (await page
            .locator("text=couldn't find")
            .count()
            .catch(() => 0)) > 0;
        if (is404) {
          console.warn(`  ⚠ 404: ${url}`);
          return null;
        }
        if (attempt < MAX_RETRIES) {
          await sleep(2_000 * attempt);
          continue;
        }
        console.warn(`  ⚠ no name found: ${url}`);
        return null;
      }

      const hq = parseHQ(data.headquarters);
      return {
        date_added: today(),
        company_name: data.name,
        company_website: data.website,
        hq_country: hq.country,
        hq_city: hq.city,
        industry: data.industry,
        sector: data.sector,
      };
    } catch (err) {
      if (attempt === MAX_RETRIES) {
        console.error(`  ✗ failed after ${MAX_RETRIES} attempts: ${url}`, err);
        return null;
      }
      await sleep(2_000 * attempt);
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Worker pool — scrape detail pages with bounded concurrency
// ---------------------------------------------------------------------------

async function processCompanyUrls(
  context: BrowserContext,
  urls: string[],
  seen: Set<string>,
): Promise<number> {
  const workers = await Promise.all(
    Array.from({ length: CONCURRENCY }, () => context.newPage()),
  );

  let nextIndex = 0;
  let saved = 0;
  const total = urls.length;

  async function work(workerPage: Page): Promise<void> {
    while (nextIndex < total) {
      const i = nextIndex++;
      const url = urls[i];
      const company = await scrapeCompanyPage(workerPage, url);
      if (company && !seen.has(company.company_name)) {
        appendCompany(company);
        seen.add(company.company_name);
        saved++;
      }
      if ((i + 1) % 50 === 0 || i + 1 === total) {
        console.log(`  progress: ${i + 1}/${total}  (${saved} saved)`);
      }
    }
  }

  await Promise.all(workers.map(work));
  await Promise.all(workers.map((w) => w.close()));
  return saved;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log("=== B Corp Directory Scraper ===");
  console.log(`Output file : ${OUTPUT_FILE}`);
  console.log(`Concurrency : ${CONCURRENCY}`);
  console.log(`Date        : ${today()}`);
  console.log();

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent:
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  });

  try {
    const seen = initCSV();

    // Phase 1 — collect company URLs
    console.log("\n--- Phase 1: Collecting company URLs from directory ---");
    const listingPage = await context.newPage();
    const companyUrls = await collectCompanyUrls(listingPage);
    await listingPage.close();
    console.log(`\nFound ${companyUrls.length} company URLs.\n`);

    if (companyUrls.length === 0) {
      console.error(
        "No company URLs found. The page structure may have changed.",
      );
      process.exit(1);
    }

    // Phase 2 — scrape each company detail page
    console.log("--- Phase 2: Scraping company detail pages ---");
    const saved = await processCompanyUrls(context, companyUrls, seen);
    console.log(`\n=== Done — ${saved} new companies saved to ${OUTPUT_FILE} ===`);
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
