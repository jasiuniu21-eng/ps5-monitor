#!/usr/bin/env python3
from __future__ import annotations
import asyncio
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

RESULTS_DIR = Path("results")
RESULTS_DIR.mkdir(exist_ok=True)

MIN_PRICE = 500
MAX_PRICE = 2000
QUERY = "ps5"

OLX_MAX_PAGES = 10

# Ceny referencyjne (nowe, sklepowe, czerwiec 2026) — używane do oceny ofert
NEW_PRICE_DIGITAL = 1899
NEW_PRICE_SLIM_DISC = 2299
NEW_PRICE_PRO = 3499

# Słowa wykluczające — to nie jest konsola, tylko gra/akcesorium
EXCLUDE_KEYWORDS = [
    # gry
    "gra ", "gry ", " gra", " gry", "płyta", "plyta", "płytka", "edycja gry",
    "kolekcjonerska", "kolekcjonerskie", "remastered", "deluxe edition",
    "callisto", "fifa", "ea sports", "spider-man", "spider man", "horizon",
    "god of war", "elden ring", "diablo", "cyberpunk", "minecraft",
    # akcesoria
    "pad ", "pady", "kontroler", "dualsense", "dual sense",
    "etui", "pokrowiec", "case ", "futerał", "naklejk", "skin ", "skiny",
    "stojak", "podstawka", "uchwyt", "ładowarka", "ladowarka", "stacja ładująca",
    "kabel", "przewód", "przewod", "hdmi", "zasilacz",
    "słuchawki", "sluchawki", "pulse 3d", "pulse elite", "headset",
    "kamera", "playstation camera", "vr2", "vr 2", "ps vr",
    "okładka", "okladka", "do konsoli", "akcesoria", "akcesorium",
    "subskrypcja", "ps plus", "playstation plus", "doładowanie", "doladowanie",
    "karta podarunkowa", "gift card", "kod aktywacyjny", "voucher",
    "dysk ssd", "ssd ", "rozszerzenie pamięci", "rozszerzenie pamieci",
    "wentylator", "chłodzenie", "chlodzenie", "cooler",
    "kierownica", "wheel", "thrustmaster", "logitech g29", "g920", "g923",
    # nie ten model
    "remote play", "psportal", "ps portal", "ps_portal",
    "ps5 portal", "playstation portal", "konsola przenośna", "konsola przenosna",
    "cfi-y", "cfi y10",
    "ps4", "ps 4", "playstation 4", "play station 4",
    "ps3", "ps 3", "playstation 3",
    "ps2", "ps 2", "playstation 2",
]

# Słowa potwierdzające że to konsola
CONSOLE_KEYWORDS = [
    "konsola", "konsole", "konsoli",
    "playstation 5", "play station 5",
    "ps5 slim", "ps5 pro", "ps5 digital", "ps5 z napędem", "ps5 z napedem",
    "ps 5 slim", "ps 5 pro", "ps 5 digital",
    "sony ps5", "sony ps 5",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

all_offers = []


def parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.]", "", text.replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def is_shipping_available(item_text: str) -> bool:
    keywords = ["wysył", "przesył", "dostaw", "kurier", "paczka", "paczkomat", "ship"]
    return any(k in item_text.lower() for k in keywords)


def is_ps5_console(title: str) -> bool:
    """Return True jeśli tytuł oferty wygląda na konsolę PS5, nie grę/akcesorium."""
    if not title:
        return False
    t = " " + title.lower() + " "

    for kw in EXCLUDE_KEYWORDS:
        if kw in t:
            return False

    if any(kw in t for kw in CONSOLE_KEYWORDS):
        return True

    # Fallback: tytuł zawiera ps5/ps 5 i słowa typu "sprzedam/komplet/zestaw" — pewnie konsola
    has_ps5 = re.search(r"\bps\s?5\b|\bplaystation\s?5\b", t) is not None
    if not has_ps5:
        return False

    console_hints = ["sprzedam ps", "ps5 +", "ps 5 +", "ps5 plus", "ps 5 plus",
                     "z padem", "z padami", "z grami", "zestaw ps", "komplet ps",
                     "ps5 825", "ps5 1tb", "ps 5 825", "ps 5 1tb",
                     "ps5 nowa", "ps5 nowy", "ps 5 nowa", "ps 5 nowy",
                     "ps5 używan", "ps5 uzywan", "ps 5 używan", "ps 5 uzywan"]
    return any(h in t for h in console_hints)


def detect_variant(title: str) -> tuple[str, float]:
    """Wykryj wariant PS5 i zwróć (etykietę, cenę nowego sprzętu w PLN)."""
    t = title.lower()
    if "pro" in t and "ps" in t:
        return ("Pro", NEW_PRICE_PRO)
    if "digital" in t or "cyfrow" in t:
        return ("Digital", NEW_PRICE_DIGITAL)
    if "slim" in t:
        return ("Slim z napędem", NEW_PRICE_SLIM_DISC)
    if "z napęd" in t or "z naped" in t or "blu-ray" in t or "blu ray" in t:
        return ("Z napędem", NEW_PRICE_SLIM_DISC)
    return ("Standard (?)", NEW_PRICE_SLIM_DISC)


def analyze_offer(offer: dict, median_price: float) -> dict:
    """Dodaj do oferty pola: variant, new_price, vs_median, vs_new, verdict, negotiation."""
    variant, new_price = detect_variant(offer["title"])
    price = offer["price"]
    vs_median = (price - median_price) / median_price * 100  # %
    vs_new = (price - new_price) / new_price * 100  # % (ujemne = taniej niż nowa)

    if vs_median <= -20:
        verdict = "🟢 ŚWIETNA OKAZJA"
    elif vs_median <= -8:
        verdict = "🟢 Dobra cena"
    elif vs_median <= 8:
        verdict = "🟡 Średnia cena"
    elif vs_median <= 20:
        verdict = "🟠 Drogo"
    else:
        verdict = "🔴 Bardzo drogo"

    # Pole do targowania = im wyżej powyżej mediany, tym większa szansa
    if vs_median > 15:
        negotiation = "wysoka (cena znacznie powyżej mediany)"
    elif vs_median > 5:
        negotiation = "średnia (cena powyżej mediany)"
    elif vs_median > -5:
        negotiation = "niska (cena ok mediany)"
    else:
        negotiation = "niska (już taniej niż średnio)"

    offer["variant"] = variant
    offer["new_price"] = new_price
    offer["vs_median_pct"] = round(vs_median, 1)
    offer["vs_new_pct"] = round(vs_new, 1)
    offer["verdict"] = verdict
    offer["negotiation"] = negotiation
    return offer


def deduplicate(offers: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for o in offers:
        key = (o.get("title", ""), o.get("price", 0))
        if key not in seen:
            seen.add(key)
            unique.append(o)
    return unique


async def scrape_olx(page) -> list[dict]:
    offers = []
    seen_ids = set()
    for page_num in range(1, OLX_MAX_PAGES + 1):
        url = (f"https://www.olx.pl/oferty/q-{QUERY}/"
               if page_num == 1 else
               f"https://www.olx.pl/oferty/q-{QUERY}/?page={page_num}")
        print(f"[OLX] Scraping page {page_num}: {url}")
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            content = await page.content()
            soup = BeautifulSoup(content, "lxml")

            items = soup.select("[data-cy='l-card']")
            if not items:
                items = soup.select("div[class*='offer'], article")

            page_added = 0
            for item in items:
                offer_id = item.get("id") or ""
                if offer_id and offer_id in seen_ids:
                    continue

                title_el = item.select_one("h4, h6, a[class*='title']")
                price_el = item.select_one("[data-testid='ad-price'], p[class*='price'], span[class*='price']")
                link_el = item.select_one("a[href*='/d/oferta'], a[href*='/oferta']")

                title = title_el.get_text(strip=True) if title_el else ""
                price_text = price_el.get_text(strip=True) if price_el else ""
                price = parse_price(price_text)
                href = link_el.get("href", "") if link_el else ""
                link = "https://www.olx.pl" + href if href.startswith("/") else href

                item_text_full = item.get_text(" ", strip=True).lower()
                shipping = is_shipping_available(item_text_full)

                if not (title and price and MIN_PRICE <= price <= MAX_PRICE):
                    continue
                if not is_ps5_console(title):
                    continue

                if offer_id:
                    seen_ids.add(offer_id)
                offers.append({
                    "source": "OLX",
                    "title": title[:200],
                    "price": price,
                    "currency": "PLN",
                    "url": link,
                    "shipping": shipping,
                    "timestamp": datetime.now().isoformat(),
                })
                page_added += 1

            print(f"[OLX] page {page_num}: +{page_added} offers (total {len(offers)})")
            # Jeśli na całej stronie nie znaleźliśmy ani jednej karty → koniec paginacji
            if not items:
                break
        except Exception as e:
            print(f"[OLX] page {page_num} error: {e}")
            break
    print(f"[OLX] Found {len(offers)} console offers")
    return offers


async def scrape_allegro(page) -> list[dict]:
    offers = []
    url = f"https://allegro.pl/listing?string={QUERY}&order=m"
    print(f"[Allegro] Scraping {url}")
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        items = soup.select("[data-box-type='offer'], article[class*='offer'], div[class*='offer']")
        if not items:
            items = soup.select("div[class*='mpof_']")

        for item in items:
            title_el = item.select_one("a[class*='title'], h2 a, a[class*='_w7z6o']")
            price_el = item.select_one("[class*='price'], span[class*='_1svub'], span[class*='_w7z6o']")
            link_el = title_el if title_el else item.select_one("a[href*='/oferta']")

            title = title_el.get_text(strip=True) if title_el else ""
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = parse_price(price_text)
            link = link_el["href"] if link_el and link_el.get("href") else ""
            if link and link.startswith("/"):
                link = "https://allegro.pl" + link

            if title and price and MIN_PRICE <= price <= MAX_PRICE and is_ps5_console(title):
                offers.append({
                    "source": "Allegro",
                    "title": title[:200],
                    "price": price,
                    "currency": "PLN",
                    "url": link,
                    "shipping": None,
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[Allegro] Error: {e}")
    print(f"[Allegro] Found {len(offers)} offers")
    return offers


async def scrape_allegro_lokalnie(page) -> list[dict]:
    offers = []
    url = f"https://allegrolokalnie.pl/oferty/q/{QUERY}"
    print(f"[Allegro Lokalnie] Scraping {url}")
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        items = soup.select("article.mlc-itembox__container")
        if not items:
            items = soup.select("article[class*='itembox'], article[class*='offer']")

        for item in items:
            link_el = item.select_one("a[href*='/oferta/']")
            title = link_el.get_text(" ", strip=True).split("Kup teraz")[0].strip() if link_el else ""
            price_el = item.select_one(".mlc-itembox__price, .ml-offer-price")
            price_text = price_el.get_text(" ", strip=True) if price_el else ""
            price = parse_price(price_text)
            link = link_el["href"] if link_el and link_el.get("href") else ""
            if link and not link.startswith("http"):
                link = "https://allegrolokalnie.pl" + link

            full_text = item.get_text(" ", strip=True).lower()
            shipping = is_shipping_available(full_text)

            if title and price and MIN_PRICE <= price <= MAX_PRICE and is_ps5_console(title):
                offers.append({
                    "source": "AllegroLokalnie",
                    "title": title[:200],
                    "price": price,
                    "currency": "PLN",
                    "url": link,
                    "shipping": shipping,
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[Allegro Lokalnie] Error: {e}")
    print(f"[Allegro Lokalnie] Found {len(offers)} offers")
    return offers


def scrape_pepper() -> list[dict]:
    offers = []
    url = f"https://www.pepper.pl/search?q={QUERY}"
    print(f"[Pepper] Scraping {url}")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"[Pepper] HTTP {resp.status_code}")
            return offers

        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select("article.thread")
        if not items:
            items = soup.select("article[class*='thread'], div[class*='thread']")

        price_re = re.compile(r"(\d{2,4}(?:[,.]\d{1,2})?)\s*zł", re.IGNORECASE)

        for item in items:
            title_el = item.select_one("a.thread-title, a.cept-tt, a.thread-link")
            link_el = title_el
            title = title_el.get_text(strip=True) if title_el else ""

            price_el = item.select_one("span.thread-price, span[class*='thread-price']")
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = parse_price(price_text)

            if price is None:
                full_text = item.get_text(" ", strip=True)
                matches = price_re.findall(full_text)
                candidates = [parse_price(m) for m in matches]
                candidates = [c for c in candidates if c and MIN_PRICE <= c <= MAX_PRICE]
                price = candidates[0] if candidates else None

            link = link_el["href"] if link_el and link_el.get("href") else ""
            if link and not link.startswith("http"):
                link = "https://www.pepper.pl" + link

            if title and price and MIN_PRICE <= price <= MAX_PRICE and is_ps5_console(title):
                offers.append({
                    "source": "Pepper",
                    "title": title[:200],
                    "price": price,
                    "currency": "PLN",
                    "url": link,
                    "shipping": None,
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[Pepper] Error: {e}")
    print(f"[Pepper] Found {len(offers)} offers")
    return offers


def save_results(offers: list[dict]):
    if not offers:
        print("[Save] No offers to save")
        return

    df = pd.DataFrame(offers)
    csv_path = RESULTS_DIR / "ps5_offers_latest.csv"
    xlsx_path = RESULTS_DIR / f"ps5_offers_{datetime.now().strftime('%Y%m%d')}.xlsx"

    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"[Save] CSV saved: {csv_path} ({len(df)} rows)")

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="PS5 Offers")
    print(f"[Save] XLSX saved: {xlsx_path}")

    summary_path = RESULTS_DIR / "summary.json"
    summary = {
        "scan_timestamp": datetime.now().isoformat(),
        "total_offers": len(offers),
        "min_price": float(df["price"].min()),
        "max_price": float(df["price"].max()),
        "avg_price": round(float(df["price"].mean()), 2),
        "by_source": df["source"].value_counts().to_dict(),
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"[Save] Summary saved: {summary_path}")

    return df


def tg_escape(s: str) -> str:
    """Escape dla MarkdownV2 (Telegram)."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", s)


def send_tg_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        print(f"[Telegram] Error: {resp.status_code} {resp.text[:300]}")
    except Exception as e:
        print(f"[Telegram] Exception: {e}")
    return False


def send_telegram(df: pd.DataFrame):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Skipping - no token or chat ID")
        return

    count = len(df)
    by_source = df["source"].value_counts().to_dict()
    price_min = df["price"].min()
    price_max = df["price"].max()
    price_median = df["price"].median()
    price_avg = df["price"].mean()

    header = (
        f"🎮 *PS5 Monitor — {tg_escape(datetime.now().strftime('%Y-%m-%d'))}*\n"
        f"Znaleziono *{count}* konsol w przedziale {MIN_PRICE}–{MAX_PRICE} zł\n\n"
        f"*Źródła:* OLX: {by_source.get('OLX', 0)} \\| Allegro: {by_source.get('Allegro', 0)} \\| "
        f"AllegroLok: {by_source.get('AllegroLokalnie', 0)} \\| Pepper: {by_source.get('Pepper', 0)}\n\n"
        f"*Rynek wtórny (z dziś):*\n"
        f"  • mediana: *{price_median:.0f} zł*\n"
        f"  • średnia: {price_avg:.0f} zł\n"
        f"  • zakres: {price_min:.0f}–{price_max:.0f} zł\n\n"
        f"*Ceny nowych \\(orientacyjnie\\):*\n"
        f"  • Digital: {NEW_PRICE_DIGITAL} zł\n"
        f"  • Slim z napędem: {NEW_PRICE_SLIM_DISC} zł\n"
        f"  • Pro: {NEW_PRICE_PRO} zł\n"
    )
    send_tg_message(header)

    # TOP oferty wg verdyktu — sortuj rosnąco po vs_median_pct (najlepsza okazja na górze)
    top = df.sort_values("vs_median_pct").head(15)

    for _, row in top.iterrows():
        title = tg_escape(str(row["title"])[:100])
        url = str(row["url"]) or ""
        link_part = f"[link]({tg_escape(url)})" if url else "brak linku"

        vs_med = row["vs_median_pct"]
        vs_new = row["vs_new_pct"]
        vs_med_str = f"\\+{vs_med:.0f}%" if vs_med > 0 else f"{vs_med:.0f}%".replace("-", "\\-")
        vs_new_str = f"\\+{vs_new:.0f}%" if vs_new > 0 else f"{vs_new:.0f}%".replace("-", "\\-")

        msg = (
            f"{tg_escape(row['verdict'])}  *{row['price']:.0f} zł*\n"
            f"_{title}_\n"
            f"Wariant: {tg_escape(str(row['variant']))} \\| {row['source']} \\| {link_part}\n"
            f"vs mediana rynku: {vs_med_str} \\| vs cena nowej: {vs_new_str}\n"
            f"💬 Pole do targowania: {tg_escape(str(row['negotiation']))}\n"
        )
        send_tg_message(msg)

    print(f"[Telegram] Sent header + {len(top)} offer messages")


def send_email(df: pd.DataFrame):
    if not EMAIL_USER or not EMAIL_PASS or not EMAIL_TO:
        print("[Email] Skipping - missing credentials")
        return

    count = len(df)
    median_price = df["price"].median()
    html = f"""<html><body style="font-family:Arial;font-size:13px;">
    <h2>🎮 PS5 Monitor – {datetime.now().strftime('%Y-%m-%d')}</h2>
    <p>Znaleziono <b>{count}</b> konsol PS5 w przedziale {MIN_PRICE}–{MAX_PRICE} zł.</p>
    <p><b>Rynek wtórny dziś:</b> mediana {median_price:.0f} zł, średnia {df['price'].mean():.0f} zł, zakres {df['price'].min():.0f}–{df['price'].max():.0f} zł.<br>
    <b>Ceny nowych:</b> Digital {NEW_PRICE_DIGITAL} zł · Slim z napędem {NEW_PRICE_SLIM_DISC} zł · Pro {NEW_PRICE_PRO} zł.</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:12px;">
    <tr style="background:#f0f0f0;"><th>Werdykt</th><th>Cena</th><th>Wariant</th><th>vs mediana</th><th>vs nowa</th><th>Źródło</th><th>Tytuł</th><th>Targowanie</th><th>Link</th></tr>
    """
    for _, row in df.sort_values("vs_median_pct").head(30).iterrows():
        url = row["url"] or "#"
        html += (f"<tr><td>{row['verdict']}</td><td><b>{row['price']:.0f} zł</b></td>"
                 f"<td>{row['variant']}</td>"
                 f"<td>{row['vs_median_pct']:+.0f}%</td>"
                 f"<td>{row['vs_new_pct']:+.0f}%</td>"
                 f"<td>{row['source']}</td>"
                 f"<td>{row['title'][:80]}</td>"
                 f"<td>{row['negotiation']}</td>"
                 f'<td><a href="{url}">otwórz</a></td></tr>')
    html += "</table></body></html>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"PS5 Monitor – {datetime.now().strftime('%Y-%m-%d')} ({count} ofert)"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        print("[Email] Sent")
    except Exception as e:
        print(f"[Email] Error: {e}")


async def main():
    print("=" * 60)
    print(f"PS5 Monitor – {datetime.now().isoformat()}")
    print(f"Price range: {MIN_PRICE}–{MAX_PRICE} zł")
    print("=" * 60)

    offers_pepper = scrape_pepper()
    all_offers.extend(offers_pepper)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        offers_olx = await scrape_olx(page)
        all_offers.extend(offers_olx)

        offers_allegro = await scrape_allegro(page)
        all_offers.extend(offers_allegro)

        offers_allegro_lokalnie = await scrape_allegro_lokalnie(page)
        all_offers.extend(offers_allegro_lokalnie)

        await browser.close()

    all_offers[:] = deduplicate(all_offers)

    print(f"\nTotal unique offers (po filtrze konsoli): {len(all_offers)}")

    if not all_offers:
        print("No offers found matching criteria.")
        save_results(all_offers)
        return

    prices = [o["price"] for o in all_offers]
    median_price = sorted(prices)[len(prices) // 2]
    for o in all_offers:
        analyze_offer(o, median_price)

    df = save_results(all_offers)

    send_telegram(df)
    send_email(df)

    print("=" * 60)
    print("Done.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
