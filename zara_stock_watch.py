import asyncio
import json
import os
from pathlib import Path

import httpx

WATCHLIST = Path("watchlist.txt")
STATE = Path("state.json")

REEF_KEY = os.getenv("REEF_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def load_watchlist():
    if not WATCHLIST.exists():
        return []

    urls = []

    for raw in WATCHLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if "zara.com/" in line:
            urls.append(line)

    return list(dict.fromkeys(urls))


def load_state():
    if not STATE.exists():
        return {}

    try:
        return json.loads(
            STATE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_state(state):
    STATE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


async def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram henüz ayarlanmamış.")
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": False
            }
        )

        response.raise_for_status()


async def fetch_product(client, url):
    if not REEF_KEY:
        raise RuntimeError("REEF_KEY tanımlı değil.")

    response = await client.post(
        "https://api.reefapi.com/zara/v1/product_detail",
        headers={
            "x-api-key": REEF_KEY,
            "content-type": "application/json"
        },
        json={
            "url": url,
            "market": "tr",
            "include_composition": False
        },
        timeout=40
    )

    response.raise_for_status()

    payload = response.json()

    if not payload.get("ok"):
        raise RuntimeError(
            f"ReefAPI hata: {payload.get('error')}"
        )

    return payload.get("data") or {}


def walk_sizes(value, results):
    if isinstance(value, dict):

        size_name = (
            value.get("name")
            or value.get("size")
            or value.get("label")
            or value.get("display_name")
        )

        stock_value = value.get("in_stock")

        if (
            stock_value is True
            and size_name
        ):
            results.append(str(size_name).strip())

        for child in value.values():
            walk_sizes(child, results)

    elif isinstance(value, list):

        for child in value:
            walk_sizes(child, results)


def find_first(data, keys):
    if isinstance(data, dict):

        for key in keys:
            if key in data and data[key] not in (
                None,
                "",
                []
            ):
                return data[key]

        for value in data.values():
            result = find_first(value, keys)

            if result not in (
                None,
                "",
                []
            ):
                return result

    elif isinstance(data, list):

        for value in data:
            result = find_first(value, keys)

            if result not in (
                None,
                "",
                []
            ):
                return result

    return None


def parse_product(data):
    sizes = []

    walk_sizes(data, sizes)

    sizes = list(
        dict.fromkeys(
            size
            for size in sizes
            if size
        )
    )

    title = find_first(
        data,
        [
            "name",
            "product_name",
            "display_name"
        ]
    )

    reference = find_first(
        data,
        [
            "display_reference",
            "reference"
        ]
    )

    price = find_first(
        data,
        [
            "amount",
            "price_amount"
        ]
    )

    currency = find_first(
        data,
        [
            "currency",
            "currency_code"
        ]
    )

    return {
        "title": str(title or "Zara ürünü"),
        "sizes": sorted(sizes),
        "reference": str(reference or ""),
        "price": price,
        "currency": str(currency or "TRY")
    }


async def main():
    urls = load_watchlist()

    if not urls:
        print("watchlist.txt boş.")
        return

    if not REEF_KEY:
        raise RuntimeError(
            "GitHub Secret olarak REEF_KEY eklenmeli."
        )

    previous = load_state()
    current = {}

    async with httpx.AsyncClient() as client:

        for index, url in enumerate(
            urls,
            start=1
        ):

            try:
                raw = await fetch_product(
                    client,
                    url
                )

                data = parse_product(raw)

                new_sizes = set(
                    data["sizes"]
                )

                old_sizes = set(
                    previous
                    .get(url, {})
                    .get("sizes", [])
                )

                current[url] = data

                print(
                    f"[{index}/{len(urls)}] "
                    f"{data['title']} -> "
                    f"{data['sizes'] or 'TÜKENDİ'}"
                )

                # SADECE:
                # TÜKENDİ -> STOK AÇILDI
                if not old_sizes and new_sizes:

                    sizes_text = ", ".join(
                        sorted(new_sizes)
                    )

                    message = (
                        "🚨 ZARA STOK AÇILDI\n\n"
                        f"{data['title']}\n"
                        f"Beden: {sizes_text}"
                    )

                    if data["price"] not in (
                        None,
                        ""
                    ):
                        message += (
                            f"\nFiyat: "
                            f"{data['price']} "
                            f"{data['currency']}"
                        )

                    if data["reference"]:
                        message += (
                            f"\nKod: "
                            f"{data['reference']}"
                        )

                    message += (
                        f"\n\n{url}"
                    )

                    await telegram_send(
                        message
                    )

            except Exception as error:

                print(
                    f"HATA: {url}\n"
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                # Bir API hatası stok yok diye
                # kaydedilmesin.
                if url in previous:
                    current[url] = previous[url]

            await asyncio.sleep(1)

    save_state(current)


if __name__ == "__main__":
    asyncio.run(main())
