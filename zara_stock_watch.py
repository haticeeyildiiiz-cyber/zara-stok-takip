import asyncio
import json
import os
import re
from pathlib import Path

import httpx

WATCHLIST = Path("watchlist.txt")
STATE_FILE = Path("state.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://www.zara.com/tr/tr/",
}


def load_watchlist():
    if not WATCHLIST.exists():
        return []

    return [
        line.strip()
        for line in WATCHLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def product_id_from_url(url):
    match = re.search(r"-p0*([0-9]+)\.html", url)

    if not match:
        match = re.search(r"-p([0-9]+)", url)

    if not match:
        raise RuntimeError(f"Ürün ID bulunamadı: {url}")

    return str(int(match.group(1)))


async def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secret'ları henüz ayarlı değil.")
        return

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
        )
        response.raise_for_status()


async def get_json(client, url):
    response = await client.get(url, headers=HEADERS)

    text = response.text

    if response.status_code in (403, 429):
        raise RuntimeError(f"ZARA_BLOCKED HTTP {response.status_code}")

    if "Access Denied" in text:
        raise RuntimeError("ZARA_BLOCKED Access Denied")

    response.raise_for_status()

    content_type = response.headers.get("content-type", "")

    if "json" not in content_type.lower():
        raise RuntimeError(
            f"JSON yerine farklı cevap geldi: {content_type}"
        )

    return response.json()


async def fetch_product_details(client, product_id):
    url = (
        "https://www.zara.com/tr/tr/products-details"
        f"?productIds={product_id}&ajax=true"
    )

    return await get_json(client, url)


def walk(obj):
    if isinstance(obj, dict):
        yield obj

        for value in obj.values():
            yield from walk(value)

    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)


def extract_product_info(data):
    title = ""
    reference = ""
    price = ""

    size_records = []

    for obj in walk(data):

        if not title:
            for key in ("name", "displayName", "productName"):
                value = obj.get(key)

                if isinstance(value, str) and len(value.strip()) > 2:
                    title = value.strip()
                    break

        if not reference:
            for key in (
                "reference",
                "displayReference",
                "productReference",
            ):
                value = obj.get(key)

                if value:
                    reference = str(value)
                    break

        if not price:
            value = obj.get("price")

            if isinstance(value, (int, float)):
                price = value

        name = (
            obj.get("name")
            or obj.get("size")
            or obj.get("label")
            or obj.get("displayName")
        )

        availability = (
            obj.get("availability")
            or obj.get("availabilityStatus")
            or obj.get("status")
        )

        sku = (
            obj.get("sku")
            or obj.get("skuId")
            or obj.get("id")
        )

        if name and sku:
            size_records.append(
                {
                    "name": str(name),
                    "sku": str(sku),
                    "availability": (
                        str(availability).upper()
                        if availability is not None
                        else ""
                    ),
                }
            )

    return {
        "title": title or "Zara ürünü",
        "reference": reference,
        "price": price,
        "size_records": size_records,
    }


def extract_available_sizes(product):
    available = []

    valid_words = (
        "IN_STOCK",
        "LOW_STOCK",
        "AVAILABLE",
        "IN STOCK",
        "LOW STOCK",
    )

    invalid_words = (
        "OUT_OF_STOCK",
        "NOT_AVAILABLE",
        "SOLD_OUT",
        "OUT OF STOCK",
    )

    for item in product["size_records"]:
        status = item["availability"]

        if any(word in status for word in invalid_words):
            continue

        if any(word in status for word in valid_words):
            available.append(item["name"])

    return sorted(set(available))


async def main():
    watchlist = load_watchlist()
    old_state = load_state()

    if not watchlist:
        raise RuntimeError("watchlist.txt boş.")

    new_state = dict(old_state)

    errors = 0
    valid_results = 0

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
    ) as client:

        for index, url in enumerate(watchlist, start=1):

            try:
                product_id = product_id_from_url(url)

                print(
                    f"[{index}/{len(watchlist)}] "
                    f"Kontrol ediliyor: {product_id}"
                )

                raw = await fetch_product_details(
                    client,
                    product_id,
                )

                product = extract_product_info(raw)

                sizes = extract_available_sizes(product)

                valid_results += 1

                previous = old_state.get(url)

                print(
                    f"{product['title']} -> "
                    f"{sizes if sizes else 'TÜKENDİ'}"
                )

                current_record = {
                    "title": product["title"],
                    "reference": product["reference"],
                    "price": product["price"],
                    "sizes": sizes,
                    "valid": True,
                }

                # İlk geçerli ölçüm sadece baseline oluşturur.
                # Eski Access Denied state'inden sahte alarm üretmez.
                previous_valid = (
                    isinstance(previous, dict)
                    and previous.get("valid") is True
                )

                if previous_valid:
                    old_sizes = set(previous.get("sizes", []))
                    new_sizes = set(sizes)

                    # SADECE TÜKENDİ -> GERÇEK BEDEN AÇILDI
                    if not old_sizes and new_sizes:

                        size_text = ", ".join(sorted(new_sizes))

                        message = (
                            "🚨 ZARA STOK AÇILDI 🚨\n\n"
                            f"{product['title']}\n"
                            f"Beden: {size_text}"
                        )

                        if product["reference"]:
                            message += (
                                f"\nKod: {product['reference']}"
                            )

                        message += f"\n\n{url}"

                        await send_telegram(message)

                new_state[url] = current_record

            except Exception as exc:
                errors += 1

                print(
                    f"HATA: {url}\n"
                    f"{type(exc).__name__}: {exc}"
                )

                # Hata olursa eski geçerli state korunur.
                # Hata = TÜKENDİ sayılmaz.

            await asyncio.sleep(2)

    if valid_results == 0:
        raise RuntimeError(
            "Hiçbir üründen geçerli Zara verisi alınamadı. "
            "State değiştirilmedi."
        )

    save_state(new_state)

    print(
        f"\nBitti. Geçerli: {valid_results}, "
        f"Hata: {errors}"
    )


if __name__ == "__main__":
    asyncio.run(main())
