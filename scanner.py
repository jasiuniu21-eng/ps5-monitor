#!/usr/bin/env python3
from __future__ import annotations
import asyncio
import json
import os
import re
import smtplib
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
STATE_DIR = Path("state")
STATE_DIR.mkdir(exist_ok=True)
SEEN_FILE = STATE_DIR / "seen.json"
SEEN_TTL_DAYS = 21  # po 21 dniach „zapominamy" o ofercie — może wróciła ze zmienioną ceną

# Powiadamiaj o padach tylko gdy verdykt ≤ "Dobra cena" — pady są masowe, filtruj spam.
CONTROLLER_NOTIFY_VERDICTS = {"🟢 ŚWIETNA OKAZJA", "🟢 Dobra cena"}

OLX_MAX_PAGES = 10
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_THROTTLE_SEC = 4.5  # ~13 req/min, mieści się w 15 RPM free tier
DETAIL_FETCH_TIMEOUT = 25000

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ============== KATEGORIE ==============

# Ceny referencyjne (nowe, sklepowe, czerwiec 2026)
NEW_PRICE_DIGITAL = 1899
NEW_PRICE_SLIM_DISC = 2299
NEW_PRICE_PRO = 3499
NEW_PRICE_DUALSENSE = 319
NEW_PRICE_DUALSENSE_EDGE = 949

# Wspólne wykluczenia (nigdy nie chcemy)
GAMES_EXCLUDE = [
    "gra ", "gry ", " gra", " gry", "płyta", "plyta", "płytka", "edycja gry",
    "kolekcjonerska", "kolekcjonerskie", "remastered", "deluxe edition",
    "callisto", "fifa", "ea sports", "spider-man", "spider man", "horizon",
    "god of war", "elden ring", "diablo", "cyberpunk", "minecraft",
]
OLDER_PS_EXCLUDE = [
    "ps4", "ps 4", "playstation 4", "play station 4",
    "ps3", "ps 3", "playstation 3",
    "ps2", "ps 2", "playstation 2",
    "xbox", "switch", "nintendo",
]

CONSOLE_EXCLUDE = GAMES_EXCLUDE + OLDER_PS_EXCLUDE + [
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
    "remote play", "psportal", "ps portal", "ps_portal",
    "ps5 portal", "playstation portal", "konsola przenośna", "konsola przenosna",
    "cfi-y", "cfi y10",
]

CONSOLE_INCLUDE = [
    "konsola", "konsole", "konsoli",
    "playstation 5", "play station 5",
    "ps5 slim", "ps5 pro", "ps5 digital", "ps5 z napędem", "ps5 z napedem",
    "ps 5 slim", "ps 5 pro", "ps 5 digital",
    "sony ps5", "sony ps 5",
]

CONTROLLER_EXCLUDE = GAMES_EXCLUDE + [
    "ps4", "ps 4", "playstation 4", "play station 4",
    "ps3", "ps 3", "playstation 3",
    "xbox", "switch", "nintendo",
    "ładowarka", "ladowarka", "stacja ładująca", "stacja ladujaca",
    "ładowanie", "ladowanie", "docking", "dock ",
    "etui", "pokrowiec", "skin ", "skiny", "naklejk", "futerał",
    "case ", "case do", "case dla", "uchwyt",
    "wymiana", "części", "czesci", "membrana", "zamiennik",
    "joystick zamiast", "naprawa", "do naprawy",
    "uszkodzony", "niesprawny", "popsuty", "do remontu",
    "kabel", "przewód", "przewod", "hdmi",
    "konsola", "konsole", "konsoli",  # nie chcemy konsoli w torze padów
    "słuchawki", "sluchawki", "headset",
    "klawiatura", "mysz", "myszka",
    "gra ", "gry ",
]

CONTROLLER_INCLUDE = [
    "dualsense", "dual sense",
    "pad ps5", "pad do ps5", "pad ps 5", "pad do ps 5",
    "kontroler ps5", "kontroler do ps5", "kontroler ps 5",
    "pad playstation 5", "kontroler playstation 5",
    "pad sony ps5", "dualsense edge",
]


def is_ps5_console(title: str) -> bool:
    if not title:
        return False
    t = " " + title.lower() + " "
    for kw in CONSOLE_EXCLUDE:
        if kw in t:
            return False
    if any(kw in t for kw in CONSOLE_INCLUDE):
        return True
    # Fallback: tytuł ma „ps5/ps 5" + słowo sugerujące konsolę
    if not re.search(r"\bps\s?5\b|\bplaystation\s?5\b", t):
        return False
    hints = ["sprzedam ps", "ps5 +", "ps 5 +", "ps5 plus", "ps 5 plus",
             "z padem", "z padami", "z grami", "zestaw ps", "komplet ps",
             "ps5 825", "ps5 1tb", "ps 5 825", "ps 5 1tb",
             "ps5 nowa", "ps5 nowy", "ps 5 nowa", "ps 5 nowy",
             "ps5 używan", "ps5 uzywan", "ps 5 używan", "ps 5 uzywan"]
    return any(h in t for h in hints)


def is_ps5_controller(title: str) -> bool:
    if not title:
        return False
    t = " " + title.lower() + " "
    for kw in CONTROLLER_EXCLUDE:
        if kw in t:
            return False
    return any(kw in t for kw in CONTROLLER_INCLUDE)


def detect_console_variant(title: str) -> tuple[str, float]:
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


def detect_controller_variant(title: str) -> tuple[str, float]:
    t = title.lower()
    if "edge" in t:
        return ("DualSense Edge", NEW_PRICE_DUALSENSE_EDGE)
    return ("DualSense", NEW_PRICE_DUALSENSE)


CATEGORIES = {
    "console": {
        "label": "Konsola PS5",
        "emoji": "🎮",
        "query": "ps5",
        "min_price": 500,
        "max_price": 2000,
        "filter": is_ps5_console,
        "variant_fn": detect_console_variant,
        "new_prices": {
            "Digital": NEW_PRICE_DIGITAL,
            "Slim z napędem": NEW_PRICE_SLIM_DISC,
            "Pro": NEW_PRICE_PRO,
        },
    },
    "controller": {
        "label": "Pad DualSense",
        "emoji": "🕹️",
        "query": "dualsense",
        "min_price": 80,
        "max_price": 350,
        "filter": is_ps5_controller,
        "variant_fn": detect_controller_variant,
        "new_prices": {
            "DualSense": NEW_PRICE_DUALSENSE,
            "DualSense Edge": NEW_PRICE_DUALSENSE_EDGE,
        },
    },
}


# ============== HELPERS ==============

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
    kws = ["wysył", "przesył", "dostaw", "kurier", "paczka", "paczkomat", "ship"]
    return any(k in item_text.lower() for k in kws)


def deduplicate(offers: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for o in offers:
        key = (o.get("category", ""), o.get("title", "").lower().strip(), round(o.get("price", 0)))
        if key not in seen:
            seen.add(key)
            unique.append(o)
    return unique


# ============== SCRAPERS ==============

async def scrape_olx(page, cat: dict) -> list[dict]:
    offers = []
    seen_ids = set()
    query = cat["query"]
    for page_num in range(1, OLX_MAX_PAGES + 1):
        url = (f"https://www.olx.pl/oferty/q-{query}/"
               if page_num == 1 else
               f"https://www.olx.pl/oferty/q-{query}/?page={page_num}")
        print(f"[OLX/{cat['label']}] page {page_num}")
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            soup = BeautifulSoup(await page.content(), "lxml")
            items = soup.select("[data-cy='l-card']") or soup.select("div[class*='offer'], article")
            page_added = 0
            for item in items:
                offer_id = item.get("id") or ""
                if offer_id and offer_id in seen_ids:
                    continue
                title_el = item.select_one("h4, h6, a[class*='title']")
                price_el = item.select_one("[data-testid='ad-price'], p[class*='price'], span[class*='price']")
                link_el = item.select_one("a[href*='/d/oferta'], a[href*='/oferta']")
                title = title_el.get_text(strip=True) if title_el else ""
                price = parse_price(price_el.get_text(strip=True) if price_el else "")
                href = link_el.get("href", "") if link_el else ""
                link = "https://www.olx.pl" + href if href.startswith("/") else href

                if not (title and price and cat["min_price"] <= price <= cat["max_price"]):
                    continue
                if not cat["filter"](title):
                    continue
                if offer_id:
                    seen_ids.add(offer_id)
                offers.append({
                    "category": cat["label"], "source": "OLX",
                    "title": title[:200], "price": price, "currency": "PLN",
                    "url": link, "shipping": is_shipping_available(item.get_text(" ", strip=True)),
                    "timestamp": datetime.now().isoformat(),
                })
                page_added += 1
            print(f"[OLX/{cat['label']}] page {page_num}: +{page_added} (total {len(offers)})")
            if not items:
                break
        except Exception as e:
            print(f"[OLX/{cat['label']}] page {page_num} error: {e}")
            break
    return offers


async def scrape_allegro(page, cat: dict) -> list[dict]:
    offers = []
    url = f"https://allegro.pl/listing?string={cat['query']}&order=m"
    print(f"[Allegro/{cat['label']}] {url}")
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        soup = BeautifulSoup(await page.content(), "lxml")
        items = (soup.select("[data-box-type='offer'], article[class*='offer']")
                 or soup.select("div[class*='mpof_']"))
        for item in items:
            title_el = item.select_one("a[class*='title'], h2 a")
            price_el = item.select_one("[class*='price']")
            title = title_el.get_text(strip=True) if title_el else ""
            price = parse_price(price_el.get_text(strip=True) if price_el else "")
            link = title_el["href"] if title_el and title_el.get("href") else ""
            if link.startswith("/"):
                link = "https://allegro.pl" + link
            if title and price and cat["min_price"] <= price <= cat["max_price"] and cat["filter"](title):
                offers.append({
                    "category": cat["label"], "source": "Allegro",
                    "title": title[:200], "price": price, "currency": "PLN",
                    "url": link, "shipping": None,
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[Allegro/{cat['label']}] error: {e}")
    return offers


async def scrape_allegro_lokalnie(page, cat: dict) -> list[dict]:
    offers = []
    url = f"https://allegrolokalnie.pl/oferty/q/{cat['query']}"
    print(f"[ALok/{cat['label']}] {url}")
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        soup = BeautifulSoup(await page.content(), "lxml")
        items = soup.select("article.mlc-itembox__container") or soup.select("article[class*='itembox']")
        for item in items:
            link_el = item.select_one("a[href*='/oferta/']")
            title = link_el.get_text(" ", strip=True).split("Kup teraz")[0].strip() if link_el else ""
            price_el = item.select_one(".mlc-itembox__price, .ml-offer-price")
            price = parse_price(price_el.get_text(" ", strip=True) if price_el else "")
            link = link_el["href"] if link_el and link_el.get("href") else ""
            if link and not link.startswith("http"):
                link = "https://allegrolokalnie.pl" + link
            if title and price and cat["min_price"] <= price <= cat["max_price"] and cat["filter"](title):
                offers.append({
                    "category": cat["label"], "source": "AllegroLokalnie",
                    "title": title[:200], "price": price, "currency": "PLN",
                    "url": link, "shipping": is_shipping_available(item.get_text(" ", strip=True)),
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[ALok/{cat['label']}] error: {e}")
    return offers


def scrape_pepper(cat: dict) -> list[dict]:
    offers = []
    url = f"https://www.pepper.pl/search?q={cat['query']}"
    print(f"[Pepper/{cat['label']}] {url}")
    try:
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
        resp = requests.get(url, headers={"User-Agent": ua}, timeout=15)
        if resp.status_code != 200:
            print(f"[Pepper/{cat['label']}] HTTP {resp.status_code}")
            return offers
        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select("article.thread")
        price_re = re.compile(r"(\d{2,4}(?:[,.]\d{1,2})?)\s*zł", re.IGNORECASE)
        for item in items:
            title_el = item.select_one("a.thread-title, a.cept-tt, a.thread-link")
            title = title_el.get_text(strip=True) if title_el else ""
            price_el = item.select_one("span.thread-price")
            price = parse_price(price_el.get_text(strip=True) if price_el else "")
            if price is None:
                cands = [parse_price(m) for m in price_re.findall(item.get_text(" ", strip=True))]
                cands = [c for c in cands if c and cat["min_price"] <= c <= cat["max_price"]]
                price = cands[0] if cands else None
            link = title_el["href"] if title_el and title_el.get("href") else ""
            if link and not link.startswith("http"):
                link = "https://www.pepper.pl" + link
            if title and price and cat["min_price"] <= price <= cat["max_price"] and cat["filter"](title):
                offers.append({
                    "category": cat["label"], "source": "Pepper",
                    "title": title[:200], "price": price, "currency": "PLN",
                    "url": link, "shipping": None,
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[Pepper/{cat['label']}] error: {e}")
    return offers


# ============== DETAIL FETCH ==============

async def fetch_description(page, source: str, url: str) -> str:
    """Wejdź na stronę oferty, wyciągnij opis. Zwróć '' przy błędzie."""
    if not url:
        return ""
    try:
        await page.goto(url, timeout=DETAIL_FETCH_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        html = await page.content()
        soup = BeautifulSoup(html, "lxml")
        if source == "OLX":
            el = (soup.select_one("[data-cy='ad_description']")
                  or soup.select_one("div[data-testid='ad-description']")
                  or soup.select_one(".css-1t507yq"))
        elif source == "AllegroLokalnie":
            el = (soup.select_one(".offer-description")
                  or soup.select_one("[class*='description']")
                  or soup.select_one("section[class*='desc']"))
        elif source == "Allegro":
            el = (soup.select_one("[data-box-name='description']")
                  or soup.select_one("[class*='description']"))
        else:
            el = soup.select_one("[class*='description'], [class*='thread-body']")
        if not el:
            return ""
        text = el.get_text(" ", strip=True)
        return text[:3000]  # limit dla Gemini
    except Exception as e:
        print(f"[detail] {source} {url[:60]} error: {e}")
        return ""


# ============== GEMINI ==============

GEMINI_PROMPT = """Jesteś ekspertem od kupowania PS5 i akcesoriów z drugiej ręki w Polsce.
Pomóż użytkownikowi wynegocjować NIŻSZĄ cenę.

Oferta:
- Tytuł: {title}
- Cena wystawiona: {price} zł
- Wariant: {variant}
- Cena nowego sprzętu w sklepie: {new_price} zł
- Mediana rynku wtórnego (z dzisiejszych ogłoszeń): {median_price} zł
- Źródło: {source}
- Pełny opis oferty:
\"\"\"
{description}
\"\"\"

Twoje zadanie — odpowiedz JSON-em (bez markdownu, bez ```json```, tylko surowy JSON) z polami:
{{
  "red_flags": ["lista krótkich obserwacji wartych wykorzystania w negocjacji - np. brak gwarancji, używana >X miesięcy, rysy, brak pudełka, sprzedawany 'bez powodu' co sugeruje wadę, drift na padzie, bateria słabnie, kabel innej marki, niejasne ślady używania, mało zdjęć, oferent unika opisu stanu, przesada w komplecie itd. - max 5"],
  "recommended_offer": liczba_PLN_jaką_zaproponować,
  "negotiation_message": "Gotowa wiadomość do wysłania sprzedającemu - po polsku, uprzejma, ale konkretnie wykorzystująca red flags i argumenty cenowe. Krótko (3-5 zdań). Bez 'Pan/Pani' jeśli to ogłoszenie OLX (tam się tyka). Zaproponuj konkretną cenę. Nie podawaj swoich danych. Brzmij naturalnie, nie jak bot.",
  "verdict_short": "jedno zdanie podsumowania czy w ogóle warto"
}}

Jeśli opis jest pusty/krótki - opieraj się tylko na tytule i statystykach rynku.
"""


def gemini_call(prompt: str) -> dict | None:
    if not GEMINI_API_KEY:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.6,
            "responseMimeType": "application/json",
        },
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"[Gemini] HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        # czasem mimo responseMimeType=json mogą być fence-y
        text = text.strip().lstrip("`").lstrip("json").strip()
        if text.endswith("```"):
            text = text.rstrip("`").strip()
        return json.loads(text)
    except Exception as e:
        print(f"[Gemini] exception: {e}")
        return None


def gemini_analyze_offer(offer: dict, median_price: float) -> dict:
    prompt = GEMINI_PROMPT.format(
        title=offer["title"],
        price=int(offer["price"]),
        variant=offer.get("variant", "?"),
        new_price=int(offer.get("new_price", 0)),
        median_price=int(median_price),
        source=offer["source"],
        description=offer.get("description", "") or "(brak opisu)",
    )
    result = gemini_call(prompt)
    if not result:
        return {"red_flags": [], "recommended_offer": None,
                "negotiation_message": "(Gemini niedostępny)", "verdict_short": ""}
    return result


# ============== PRICE ANALYSIS ==============

def attach_market_analysis(offer: dict, median_price: float, variant_fn) -> dict:
    variant, new_price = variant_fn(offer["title"])
    price = offer["price"]
    vs_median = (price - median_price) / median_price * 100
    vs_new = (price - new_price) / new_price * 100
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
    offer["variant"] = variant
    offer["new_price"] = new_price
    offer["vs_median_pct"] = round(vs_median, 1)
    offer["vs_new_pct"] = round(vs_new, 1)
    offer["verdict"] = verdict
    return offer


# ============== SEEN STATE ==============

def load_seen() -> dict:
    """Zwróć dict {url: iso_timestamp} ofert już zaalertowanych."""
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text())
        if not isinstance(data, dict):
            return {}
        # Usuń wpisy starsze niż TTL
        cutoff = datetime.now().timestamp() - SEEN_TTL_DAYS * 86400
        return {k: v for k, v in data.items()
                if datetime.fromisoformat(v).timestamp() > cutoff}
    except Exception as e:
        print(f"[seen] load error: {e}")
        return {}


def save_seen(seen: dict):
    try:
        SEEN_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False))
        print(f"[seen] saved {len(seen)} entries to {SEEN_FILE}")
    except Exception as e:
        print(f"[seen] save error: {e}")


def offer_key(offer: dict) -> str:
    """Klucz identyfikujący ofertę — preferuj URL, fallback na title+price."""
    url = offer.get("url") or ""
    if url:
        return url
    return f"{offer.get('source','')}::{offer.get('title','')}::{offer.get('price',0)}"


# ============== STORAGE ==============

def save_results(all_offers: list[dict]):
    if not all_offers:
        print("[Save] nothing to save")
        return None
    df = pd.DataFrame(all_offers)
    csv_path = RESULTS_DIR / "ps5_offers_latest.csv"
    xlsx_path = RESULTS_DIR / f"ps5_offers_{datetime.now().strftime('%Y%m%d')}.xlsx"
    df.to_csv(csv_path, index=False, encoding="utf-8")
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Offers")
    summary = {
        "scan_timestamp": datetime.now().isoformat(),
        "total_offers": len(all_offers),
        "by_category": df["category"].value_counts().to_dict(),
        "by_source": df["source"].value_counts().to_dict(),
    }
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[Save] {csv_path} ({len(df)} rows), {xlsx_path}, summary.json")
    return df


# ============== TELEGRAM ==============

def tg_escape(s: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(s))


def send_tg_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram limit: 4096 chars per message
    if len(text) > 4000:
        text = text[:4000] + "…"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": text,
            "parse_mode": "MarkdownV2", "disable_web_page_preview": True,
        }, timeout=15)
        if r.status_code == 200:
            return True
        print(f"[Telegram] {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"[Telegram] exc: {e}")
    return False


def fmt_pct(v: float) -> str:
    if v > 0:
        return f"\\+{v:.0f}%"
    return f"{v:.0f}%".replace("-", "\\-")


def send_category_telegram(cat_label: str, cat_emoji: str, df_cat: pd.DataFrame, new_prices: dict):
    if df_cat.empty:
        return
    pmin, pmax, pmed, pavg = df_cat["price"].min(), df_cat["price"].max(), df_cat["price"].median(), df_cat["price"].mean()
    by_src = df_cat["source"].value_counts().to_dict()

    new_lines = "\n".join(f"  • {k}: {v} zł" for k, v in new_prices.items())
    header = (
        f"{cat_emoji} *{tg_escape(cat_label)} — {tg_escape(datetime.now().strftime('%Y-%m-%d'))}*\n"
        f"Znaleziono *{len(df_cat)}* ofert\n\n"
        f"*Źródła:* " + " \\| ".join(f"{tg_escape(s)}: {n}" for s, n in by_src.items()) + "\n\n"
        f"*Rynek wtórny \\(dziś\\):*\n"
        f"  • mediana: *{pmed:.0f} zł*\n"
        f"  • średnia: {pavg:.0f} zł\n"
        f"  • zakres: {pmin:.0f}–{pmax:.0f} zł\n\n"
        f"*Ceny nowych \\(orientacyjnie\\):*\n{tg_escape(new_lines).replace(chr(92)+chr(92)+'.', '.').replace(chr(92)+'.', '.')}\n"
    )
    # tg_escape already escaped \n inside new_lines, fix manually
    new_lines_esc = "\n".join(f"  • {tg_escape(k)}: {v} zł" for k, v in new_prices.items())
    header = (
        f"{cat_emoji} *{tg_escape(cat_label)} — {tg_escape(datetime.now().strftime('%Y-%m-%d'))}*\n"
        f"Znaleziono *{len(df_cat)}* ofert\n\n"
        f"*Źródła:* " + " \\| ".join(f"{tg_escape(s)}: {n}" for s, n in by_src.items()) + "\n\n"
        f"*Rynek wtórny \\(dziś\\):*\n"
        f"  • mediana: *{pmed:.0f} zł*\n"
        f"  • średnia: {pavg:.0f} zł\n"
        f"  • zakres: {pmin:.0f}–{pmax:.0f} zł\n\n"
        f"*Ceny nowych \\(orientacyjnie\\):*\n{new_lines_esc}\n"
    )
    send_tg_message(header)

    for _, row in df_cat.sort_values("vs_median_pct").iterrows():
        title = tg_escape(str(row["title"])[:120])
        url_ = str(row.get("url") or "")
        link_part = f"[otwórz ofertę]({tg_escape(url_)})" if url_ else "brak linku"
        vs_med = row["vs_median_pct"]
        vs_new = row["vs_new_pct"]

        msg = (
            f"{tg_escape(row['verdict'])}  *{row['price']:.0f} zł*\n"
            f"_{title}_\n"
            f"Wariant: {tg_escape(row['variant'])} \\| {tg_escape(row['source'])} \\| {link_part}\n"
            f"vs mediana: {fmt_pct(vs_med)} \\| vs cena nowej: {fmt_pct(vs_new)}\n"
        )

        red_flags = row.get("red_flags") or []
        if isinstance(red_flags, str):
            try:
                red_flags = json.loads(red_flags)
            except Exception:
                red_flags = []
        if red_flags:
            msg += "\n🚩 *Argumenty do negocjacji:*\n"
            for f in red_flags[:5]:
                msg += f"  • {tg_escape(str(f))}\n"

        rec = row.get("recommended_offer")
        if rec and not pd.isna(rec):
            try:
                msg += f"\n💰 *Proponuj:* {int(float(rec))} zł\n"
            except Exception:
                pass

        verdict_short = row.get("verdict_short")
        if verdict_short and not pd.isna(verdict_short) and str(verdict_short).strip():
            msg += f"_{tg_escape(str(verdict_short))}_\n"

        neg_msg = row.get("negotiation_message")
        if neg_msg and not pd.isna(neg_msg) and str(neg_msg).strip() and str(neg_msg) != "(Gemini niedostępny)":
            msg += f"\n📨 *Wiadomość do skopiowania:*\n```\n{neg_msg}\n```\n"

        send_tg_message(msg)


def send_telegram_all(df: pd.DataFrame):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] skipping — no token/chat")
        return
    for cat_key, cat in CATEGORIES.items():
        sub = df[df["category"] == cat["label"]]
        send_category_telegram(cat["label"], cat["emoji"], sub, cat["new_prices"])
    print(f"[Telegram] sent {len(df)} offers across categories")


# ============== EMAIL ==============

def send_email(df: pd.DataFrame):
    if not (EMAIL_USER and EMAIL_PASS and EMAIL_TO):
        print("[Email] skipping — missing creds")
        return
    html_parts = [f"<html><body style='font-family:Arial;font-size:13px;'>",
                  f"<h2>🎮 PS5 Monitor – {datetime.now().strftime('%Y-%m-%d')}</h2>"]
    for cat_key, cat in CATEGORIES.items():
        sub = df[df["category"] == cat["label"]]
        if sub.empty:
            continue
        html_parts.append(f"<h3>{cat['emoji']} {cat['label']} — {len(sub)} ofert (mediana {sub['price'].median():.0f} zł)</h3>")
        html_parts.append("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:12px;'>")
        html_parts.append("<tr style='background:#f0f0f0;'><th>Werdykt</th><th>Cena</th><th>Wariant</th><th>vs mediana</th><th>vs nowa</th><th>Tytuł</th><th>Argumenty</th><th>Propozycja</th><th>Wiadomość</th><th>Link</th></tr>")
        for _, r in sub.sort_values("vs_median_pct").iterrows():
            flags = r.get("red_flags") or []
            flags_html = "<br>".join(f"• {str(f)}" for f in (flags if isinstance(flags, list) else []))
            msg = (r.get("negotiation_message") or "").replace("\n", "<br>")
            rec = r.get("recommended_offer")
            rec_html = f"{int(float(rec))} zł" if rec and not pd.isna(rec) else ""
            html_parts.append(
                f"<tr><td>{r['verdict']}</td><td><b>{r['price']:.0f} zł</b></td>"
                f"<td>{r['variant']}</td><td>{r['vs_median_pct']:+.0f}%</td>"
                f"<td>{r['vs_new_pct']:+.0f}%</td><td>{r['title'][:80]}</td>"
                f"<td>{flags_html}</td><td>{rec_html}</td>"
                f"<td style='max-width:300px;'>{msg}</td>"
                f"<td><a href='{r.get('url') or '#'}'>otwórz</a></td></tr>"
            )
        html_parts.append("</table>")
    html_parts.append("</body></html>")
    html = "\n".join(html_parts)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"PS5 Monitor – {datetime.now().strftime('%Y-%m-%d')} ({len(df)} ofert)"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.send_message(msg)
        print("[Email] sent")
    except Exception as e:
        print(f"[Email] error: {e}")


# ============== MAIN ==============

async def main():
    print("=" * 60)
    print(f"PS5 Monitor – {datetime.now().isoformat()}")
    print(f"Gemini: {'ON' if GEMINI_API_KEY else 'OFF'}")
    print("=" * 60)

    all_offers: list[dict] = []

    # Pepper (synchronously, requests-based) — per category
    for cat in CATEGORIES.values():
        all_offers.extend(scrape_pepper(cat))

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()

        for cat in CATEGORIES.values():
            all_offers.extend(await scrape_olx(page, cat))
            all_offers.extend(await scrape_allegro(page, cat))
            all_offers.extend(await scrape_allegro_lokalnie(page, cat))

        # Dedup w obrębie skanu + analiza cenowa per kategoria
        all_offers = deduplicate(all_offers)
        print(f"\nTotal unique offers in this scan: {len(all_offers)}")

        # Mediany per kategoria — z PEŁNEGO zbioru (zanim odfiltrujemy seen)
        cat_medians = {}
        for cat in CATEGORIES.values():
            prices = [o["price"] for o in all_offers if o["category"] == cat["label"]]
            cat_medians[cat["label"]] = sorted(prices)[len(prices) // 2] if prices else 0
            print(f"  mediana {cat['label']}: {cat_medians[cat['label']]} zł")

        for o in all_offers:
            cat = next((c for c in CATEGORIES.values() if c["label"] == o["category"]), None)
            if cat:
                attach_market_analysis(o, cat_medians[cat["label"]], cat["variant_fn"])

        # Filtr 1: pomiń oferty już zaalertowane w poprzednich skanach
        seen = load_seen()
        fresh = [o for o in all_offers if offer_key(o) not in seen]
        print(f"\nSeen filter: {len(all_offers) - len(fresh)} pominięto (były już alertowane), {len(fresh)} świeżych")

        # Filtr 2: dla padów — tylko świetne/dobre okazje
        to_alert = []
        for o in fresh:
            if o["category"] == "Pad DualSense":
                if o["verdict"] in CONTROLLER_NOTIFY_VERDICTS:
                    to_alert.append(o)
            else:
                to_alert.append(o)
        print(f"Quality filter: {len(to_alert)} ofert do analizy/powiadomienia (pady tylko 🟢)")

        # Pobierz opis + Gemini analiza tylko dla finalnej listy
        if GEMINI_API_KEY and to_alert:
            print(f"\n[Detail+Gemini] processing {len(to_alert)} offers...")
            for i, offer in enumerate(to_alert, 1):
                print(f"  [{i}/{len(to_alert)}] {offer['source']} — {offer['title'][:50]}")
                desc = await fetch_description(page, offer["source"], offer["url"])
                offer["description"] = desc
                analysis = gemini_analyze_offer(offer, cat_medians[offer["category"]])
                offer["red_flags"] = analysis.get("red_flags", [])
                offer["recommended_offer"] = analysis.get("recommended_offer")
                offer["negotiation_message"] = analysis.get("negotiation_message", "")
                offer["verdict_short"] = analysis.get("verdict_short", "")
                await asyncio.sleep(GEMINI_THROTTLE_SEC)

        await browser.close()

    # Zapisz pełny CSV wszystkich ofert ze skanu, ale notyfikuj tylko nowe
    save_results(all_offers)

    if not to_alert:
        print("Brak nowych ofert do powiadomienia.")
    else:
        df_alert = pd.DataFrame(to_alert)
        send_telegram_all(df_alert)
        send_email(df_alert)

    # Zapisz state: oznacz wszystkie zaalertowane oferty jako widziane
    now_iso = datetime.now().isoformat()
    for o in to_alert:
        seen[offer_key(o)] = now_iso
    save_seen(seen)

    print("=" * 60)
    print("Done.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
