import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print("Navigating to halandseabourn.pinpointhq.com...")
        res = await page.goto("https://halandseabourn.pinpointhq.com/", wait_until="networkidle", timeout=30000)
        print("Status:", res.status if res else None)
        print("Title:", await page.title())
        
        # Look for posting cards
        links = await page.query_selector_all("a[href*='/en/postings/'], a[href*='postings']")
        print(f"Found {len(links)} postings links!")
        for l in links[:15]:
            text = (await l.inner_text()).strip()
            href = await l.get_attribute("href")
            full_href = f"https://halandseabourn.pinpointhq.com{href}" if href.startswith("/") else href
            print(f" - {text.replace(chr(10), ' | ')} -> {full_href}")
            
        await page.screenshot(path="seabourn_portal.png")
        print("Saved screenshot to seabourn_portal.png")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
