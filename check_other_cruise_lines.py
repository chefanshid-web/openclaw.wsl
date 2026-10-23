import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

targets = [
    ("Silversea Cruises", "https://www.allcruisejobs.com/jobs/silversea/"),
    ("Explora Journeys", "https://www.allcruisejobs.com/jobs/explora-journeys/"),
    ("Viking Cruises", "https://www.allcruisejobs.com/jobs/viking/"),
]

async def check():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        for name, url in targets:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")
                links = soup.find_all("a", href=lambda h: h and "/i" in h)
                print(f"\n=== {name} ({len(links)} total postings) ===")
                culinary_count = 0
                for l in links:
                    t = l.get_text(strip=True)
                    href = l.get("href", "")
                    full_href = href if href.startswith("http") else f"https://www.allcruisejobs.com{href}"
                    if any(k in t.lower() for k in ["chef", "cook", "butcher", "galley", "culinary", "demi", "partie", "cuisine"]):
                        culinary_count += 1
                        print(f" * {t} -> {full_href}")
                if culinary_count == 0:
                    print(f" (No active culinary openings currently listed on {name} dedicated board)")
            except Exception as e:
                print(f"Error checking {name}: {e}")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(check())
