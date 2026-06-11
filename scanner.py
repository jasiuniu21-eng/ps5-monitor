#!/usr/bin/env python3
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
    url = f"https://www.olx.pl/oferty/q-{QUERY}/"
    print(f"[OLX] Scraping {url}")
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        items = soup.select("[data-cy='l-card'], div[class*='offer'], article")
        if not items:
            items = soup.select("li[class*='offer'], div[class*='listing'] div[class*='card']")

        for item in items:
            title_el = item.select_one("h6, a[class*='title'], h4")
            price_el = item.select_one("[data-testid='ad-price'], p[class*='price'], span[class*='price']")
            link_el = item.select_one("a[href*='/oferta']")

            title = title_el.get_text(strip=True) if title_el else ""
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = parse_price(price_text)
            link = "https://www.olx.pl" + link_el["href"] if link_el and link_el.get("href","").startswith("/oferta") else ""

            item_text_full = item.get_text(" ", strip=True).lower()
            shipping = is_shipping_available(item_text_full)

            if title and price and MIN_PRICE <= price <= MAX_PRICE:
                offers.append({
                    "source": "OLX",
                    "title": title[:200],
                    "price": price,
                    "currency": "PLN",
                    "url": link,
                    "shipping": shipping,
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[OLX] Error: {e}")
    print(f"[OLX] Found {len(offers)} offers")
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

            if title and price and MIN_PRICE <= price <= MAX_PRICE:
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
    url = f"https://allegrolokalnie.pl/oferty?q={QUERY}&order=newest"
    print(f"[Allegro Lokalnie] Scraping {url}")
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        items = soup.select("article[class*='offer'], div[class*='offer-card'], li[class*='offer']")
        if not items:
            items = soup.select("div[data-testid='offer'], div[class*='_offer']")

        for item in items:
            title_el = item.select_one("h2 a, a[class*='title'], h3 a")
            price_el = item.select_one("span[class*='price'], span[class*='value']")
            link_el = item.select_one("a[href*='/oferta']")
            desc_el = item.select_one("p[class*='description'], div[class*='description'], span[class*='desc']")

            title = title_el.get_text(strip=True) if title_el else ""
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = parse_price(price_text)
            link = link_el["href"] if link_el and link_el.get("href") else ""
            if link and not link.startswith("http"):
                link = "https://allegrolokalnie.pl" + link

            full_text = item.get_text(" ", strip=True).lower()
            shipping = is_shipping_available(full_text)

            if title and price and MIN_PRICE <= price <= MAX_PRICE:
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
        items = soup.select("article.thread, div.thread, li.thread")
        if not items:
            items = soup.select("[data-testid='thread'], div[class*='thread']")

        for item in items:
            title_el = item.select_one("a[class*='thread-title'], h2 a, a[class*='title']")
            price_el = item.select_one("span[class*='thread-price'], span[class*='price']")
            link_el = item.select_one("a[class*='thread-title']")

            title = title_el.get_text(strip=True) if title_el else ""
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = parse_price(price_text)
            link = link_el["href"] if link_el and link_el.get("href") else ""
            if link and not link.startswith("http"):
                link = "https://www.pepper.pl" + link

            if title and price and MIN_PRICE <= price <= MAX_PRICE:
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


def send_telegram(df: pd.DataFrame):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Skipping - no token or chat ID")
        return

    count = len(df)
    by_source = df["source"].value_counts().to_dict()
    price_min = df["price"].min()
    price_max = df["price"].max()

    message = (
        f"🎮 *PS5 Monitor – {datetime.now().strftime('%Y-%m-%d')}*\n"
        f"Znaleziono {count} ofert w przedziale {MIN_PRICE}–{MAX_PRICE} zł\n\n"
        f"*Źródła:*\n"
        f"  OLX: {by_source.get('OLX', 0)}\n"
        f"  Allegro: {by_source.get('Allegro', 0)}\n"
        f"  Allegro Lokalnie: {by_source.get('AllegroLokalnie', 0)}\n"
        f"  Pepper: {by_source.get('Pepper', 0)}\n\n"
        f"*Ceny:*\n"
        f"  Min: {price_min:.0f} zł\n"
        f"  Max: {price_max:.0f} zł\n"
        f"  Średnia: {df['price'].mean():.0f} zł\n"
    )

    if len(df) <= 10:
        message += f"\n*Najlepsze oferty:*\n"
        top = df.sort_values("price").head(10)
        for _, row in top.iterrows():
            message += f"  • {row['title'][:60]} – {row['price']:.0f} zł ({row['source']})\n"
    else:
        top = df.sort_values("price").head(5)
        message += f"\n*Top 5 najtańszych:*\n"
        for _, row in top.iterrows():
            message += f"  • {row['title'][:60]} – {row['price']:.0f} zł ({row['source']})\n"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("[Telegram] Message sent")
        else:
            print(f"[Telegram] Error: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[Telegram] Exception: {e}")


def send_email(df: pd.DataFrame):
    if not EMAIL_USER or not EMAIL_PASS or not EMAIL_TO:
        print("[Email] Skipping - missing credentials")
        return

    count = len(df)
    html = f"""<html><body>
    <h2>🎮 PS5 Monitor – {datetime.now().strftime('%Y-%m-%d')}</h2>
    <p>Znaleziono <b>{count}</b> ofert w przedziale {MIN_PRICE}–{MAX_PRICE} zł.</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:Arial;font-size:13px;">
    <tr style="background:#f0f0f0;"><th>Źródło</th><th>Tytuł</th><th>Cena</th><th>Link</th></tr>
    """
    for _, row in df.sort_values("price").head(20).iterrows():
        url = row["url"] or "#"
        html += f"<tr><td>{row['source']}</td><td>{row['title'][:80]}</td><td>{row['price']:.0f} zł</td>"
        html += f'<td><a href="{url}">Link</a></td></tr>'
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

    print(f"\nTotal unique offers: {len(all_offers)}")

    if not all_offers:
        print("No offers found matching criteria.")
        save_results(all_offers)
        return

    df = save_results(all_offers)

    send_telegram(df)
    send_email(df)

    print("=" * 60)
    print("Done.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
