import asyncio
import json
import os
import re
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

WATCHLIST = Path("watchlist.txt")
STATE = Path("state.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

IN_STOCK_ACTIONS = {"size-in-stock", "size-low-on-stock"}


def load_watchlist():
    if not WATCHLIST.exists():
        return []

    urls = []
    for raw in WATCHLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if "zara.com/tr/tr/" in line:
            urls.append(line)

    return list(dict.fromkeys(urls))


def load_state():
    if not STATE.exists():
        return {}

    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


async def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram ayarlanmamış.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
        )

        response.raise_for_status()


async def inspect_product(page, url):
    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000
    )

    await page.wait_for_timeout(1500)

    title = "Zara ürünü"

    for selector in [
        "h1",
        '[data-qa-qualifier="product-detail-info-name"]',
        '[data-qa-qualifier="product-detail-name"]',
    ]:
        try:
            element = page.locator(selector).first

            if await element.count():
                text = (await element.inner_text()).strip()

                if text:
                    title = text
                    break
        except Exception:
            pass

    price = ""

    for selector in [
        '[data-qa-qualifier="price-amount-current"]',
        '[data-qa-qualifier="price-amount"]',
        ".money-amount__main",
    ]:
        try:
            element = page.locator(selector).first

            if await element.count():
                text = (await element.inner_text()).strip()

                if text:
                    price = text
                    break
        except Exception:
            pass

    body_text = ""

    try:
        body_text = (
            await page.locator("body").inner_text()
        ).lower()
    except Exception:
        pass

    coming_soon = (
        "coming soon" in body_text
        or "yakında" in body_text
    )

    try:
        add_button = page.locator(
            '[data-qa-action="add-to-cart"]'
        ).first

        if await add_button.count():
            await add_button.click(timeout=4000)

    except Exception:
        try:
            button = page.get_by_role(
                "button",
                name=re.compile(
                    r"sepete ekle|add to bag",
                    re.I
                ),
            ).first

            if await button.count():
                await button.click(timeout=4000)

        except Exception:
            pass

    await page.wait_for_timeout(1000)

    sizes = []

    locator = page.locator(
        '[data-qa-action="size-in-stock"], '
        '[data-qa-action="size-low-on-stock"]'
    )

    try:
        count = await locator.count()
    except Exception:
        count = 0

    for i in range(count):
        element = locator.nth(i)

        try:
            text = (await element.inner_text()).strip()

            if not text:
                text = (
                    await element.get_attribute("aria-label")
                    or ""
                ).strip()

            if text:
                sizes.append(text)

        except Exception:
            continue

    sizes = list(dict.fromkeys(sizes))

    if coming_soon:
        sizes = []

    return {
        "title": title,
        "price": price,
        "sizes": sizes,
        "coming_soon": coming_soon,
    }


async def main():
    urls = load_watchlist()

    if not urls:
        print("watchlist.txt boş.")
        return

    previous = load_state()
    current = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True
        )

        context = await browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
        )

        page = await context.new_page()

        for index, url in enumerate(urls, start=1):
            try:
                data = await inspect_product(page, url)

                old_sizes = set(
                    previous.get(url, {}).get(
                        "sizes",
                        []
                    )
                )

                new_sizes = set(data["sizes"])

                current[url] = data

                print(
                    f"[{index}/{len(urls)}] "
                    f"{data['title']} -> "
                    f"{data['sizes'] or 'TÜKENDİ'}"
                )

                if not old_sizes and new_sizes:
                    sizes_text = ", ".join(
                        sorted(new_sizes)
                    )

                    message = (
                        "🚨 ZARA STOK DÖNÜŞÜ\n"
                        f"{data['title']}\n"
                        f"Beden: {sizes_text}"
                    )

                    if data["price"]:
                        message += (
                            f"\nFiyat: {data['price']}"
                        )

                    message += f"\n{url}"

                    await telegram_send(message)

            except Exception as error:
                print(
                    f"HATA: {url} "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                if url in previous:
                    current[url] = previous[url]

            await page.wait_for_timeout(1800)

        await browser.close()

    save_state(current)


if __name__ == "__main__":
    asyncio.run(main())
