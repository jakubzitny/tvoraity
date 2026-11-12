#!/usr/bin/env python3
"""Crawl HackNitra project URLs with a real browser and export evidence.

Setup:
    python -m pip install playwright
    python -m playwright install chromium
Run:
    python scripts/crawl_projects.py
    python scripts/crawl_projects.py --screenshots --headed

Outputs: crawl-results/projects.json and crawl-results/report.md
A successful HTTP response alone is not proof that an app works.
"""
import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "projekty-build-day-2026.md"
OUTPUT = ROOT / "crawl-results"

# URLs previously shared but removed from the published Markdown.
# Keeping them here allows us to retest and potentially restore them.
EXTRA = {
    "Berry Harvest APP": ["https://d1mz04ixm0vrwrd2k5p30rd9.macaly.dev/pitch"],
    "Portál podkladov Happybaby": ["https://bohlhs06wgpdu6iuirw3ovqh.macaly.dev/"],
    "TowerBlock Advanced": [
        "https://claude.ai/artifact/94UV78esDgEWhyLf5ecMX6",
        "https://claude.ai/artifact/Pqkuu6NYt6K2Qrgncd87cK",
    ],
    "Rozvrh": ["https://claude.ai/artifact/XZCDrQBTEsTN78qbb1JTwU"],
    "Portál pre účtovníkov": ["https://m36ok1sv2tvi52c8x0s6w8lr.macaly.dev/"],
    "BrickByBrick": ["https://claude.ai/artifact/2e9pybqHMcKo9XLiEgMuJp"],
    "Projekt Romana Sládka": ["https://by68hs786b9dqdemw7fmjl40.macaly.dev/#kontakt"],
    "FolkStore": ["https://meqd889qddlr05klstqjkqdb.macaly.dev/pitch"],
    "Corporate Survival": ["https://s71i6tisgr6iv5psk97cswwf.macaly.dev/"],
    "Projekt Jakuba Refku": ["https://www.macaly.com/chat/q6x1rlm22xg4kv4sspnw9gf2"],
}

def collect_targets():
    text = DOCUMENT.read_text(encoding="utf-8")
    current = None
    projects = {}
    for line in text.splitlines():
        match = re.match(r"^## \d+\. (.+)$", line)
        if match:
            current = match.group(1)
            projects.setdefault(current, [])
        if current:
            for url in re.findall(r"\]\((https?://[^)]+)\)", line):
                projects[current].append(url)
    for name, urls in EXTRA.items():
        projects.setdefault(name, []).extend(urls)
    return [(name, url) for name, urls in projects.items() for url in dict.fromkeys(urls)]

async def inspect(browser, name, url, semaphore, timeout, screenshots):
    async with semaphore:
        page = await browser.new_page(viewport={"width": 1365, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))
        result = {"project": name, "url": url, "checked_at": datetime.now(timezone.utc).isoformat()}
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except BrowserTimeout:
                pass
            await page.wait_for_timeout(1200)
            result["status"] = response.status if response else None
            result["final_url"] = page.url
            result["title"] = (await page.title()).strip()
            result["description"] = await page.locator('meta[name="description"]').first.get_attribute("content") if await page.locator('meta[name="description"]').count() else None
            result["h1"] = (await page.locator("h1").all_text_contents())[:5]
            result["h2"] = (await page.locator("h2").all_text_contents())[:12]
            result["text"] = (await page.locator("body").inner_text(timeout=8000))[:10000]
            result["links"] = (await page.locator("a[href]").evaluate_all(
                "(els) => els.slice(0, 40).map(e => ({text: (e.innerText || '').trim().slice(0,80), href: e.href}))"
            ))
            result["page_errors"] = errors[:8]
            text = result["text"].lower()
            blocked = any(s in text[:1500] for s in (
                "sign in to continue", "login required", "access denied",
                "this deployment is protected", "authentication required",
                "application error", "404: not found",
            ))
            result["verdict"] = (
                "HTTP_ERROR" if result["status"] and result["status"] >= 400
                else "BLOCKED_OR_ERROR_PAGE" if blocked
                else "REVIEW" if not result["text"].strip()
                else "REACHABLE"
            )
            if screenshots and result["verdict"] == "REACHABLE":
                safe = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
                path = OUTPUT / "screenshots" / f"{safe}-{abs(hash(url)) % 100000:05d}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(path), full_page=True)
                result["screenshot"] = str(path.relative_to(ROOT))
        except Exception as exc:
            result["verdict"] = "FETCH_FAILED"
            result["error"] = str(exc)[:500]
        finally:
            await page.close()
        return result

async def run(args):
    OUTPUT.mkdir(exist_ok=True)
    targets = collect_targets()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        semaphore = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(*[
            inspect(browser, name, url, semaphore, args.timeout * 1000, args.screenshots)
            for name, url in targets
        ])
        await browser.close()
    (OUTPUT / "projects.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Project crawl report", "", f"Checked: {len(results)} URLs", "",
             "| Project | Result | HTTP | Page title | URL |",
             "|---|---|---:|---|---|"]
    for r in results:
        clean = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")[:90]
        lines.append(f"| {clean(r['project'])} | {r['verdict']} | {r.get('status', '')} | {clean(r.get('title'))} | {r['url']} |")
    lines += ["", "## Interpretation", "",
              "- REACHABLE: page loaded and exposed visible text; **not** proof that the application works end-to-end.",
              "- REVIEW: page responded but no meaningful visible text was extracted.",
              "- HTTP_ERROR: server returned an HTTP error.",
              "- BLOCKED_OR_ERROR_PAGE: likely login, protection or error screen.",
              "- FETCH_FAILED: network, DNS, TLS, timeout or browser failure; **not** proof the site is dead.",
              "", "Use `projects.json` for extracted page text, headings, metadata and links before editing descriptions."]
    (OUTPUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Checked {len(results)} URLs. Results: {OUTPUT / 'report.md'}")
    for r in results:
        print(f"{r['verdict']:22} {r['project']:28} {r['url']}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=25, help="Navigation timeout in seconds")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--headed", action="store_true")
    asyncio.run(run(parser.parse_args()))
