# PS5 Monitor

Automatyczne monitorowanie ofert PS5 na OLX, Allegro, Allegro Lokalnie i Pepper.pl.

## Wymagane Secrets GitHub

Po pushu repozytorium na GitHub, skonfiguruj następujące **repository secrets** (`Settings → Secrets and variables → Actions`):

| Secret | Opis | Wymagany |
|--------|------|----------|
| `TELEGRAM_BOT_TOKEN` | Token bota Telegram (od [@BotFather](https://t.me/BotFather)) | Nie |
| `TELEGRAM_CHAT_ID` | ID czatu/kanału Telegram | Nie |
| `EMAIL_USER` | Adres e-mail Gmail (nadawca) | Nie |
| `EMAIL_PASS` | Hasło aplikacji Gmail (nie zwykłe hasło) | Nie |
| `EMAIL_TO` | Adres e-mail odbiorcy powiadomień | Nie |

> Przynajmniej jeden kanał powiadomień (Telegram lub e-mail) musi być skonfigurowany.

## Jak skonfigurować Gmail hasło aplikacji
1. Włącz uwierzytelnianie dwuskładnikowe na koncie Google
2. Wejdź w [Bezpieczeństwo → Hasła aplikacji](https://myaccount.google.com/apppasswords)
3. Wygeneruj hasło dla "Poczta" i wklej jako `EMAIL_PASS`

## Jak zdobyć Telegram Chat ID
1. Stwórz bota przez [@BotFather](https://t.me/BotFather) → zapisz token
2. Wyślij wiadomość do bota
3. Odwiedź `https://api.telegram.org/bot<TOKEN>/getUpdates` → znajdź `chat.id`

## Uruchomienie lokalne

```bash
pip install -r requirements.txt
playwright install chromium
python scanner.py
```

## Jak to działa

- Workflow GitHub Actions odpala się codziennie o **5:00 UTC** (7:00 czasu polskiego)
- Skrypt scrapuje 4 portale w poszukiwaniu PS5 w cenie **500–2000 zł**
- Wyniki zapisywane są jako CSV, Excel i JSON w katalogu `results/`
- Powiadomienia wysyłane są przez Telegram i/lub e-mail
- Można też uruchomić ręcznie przez GitHub → Actions → daily-ps5-scan → Run workflow

## Ograniczenia

- **Allegro (główne)** używa DataDome (anti-bot) i często zwraca captcha zamiast wyników — bez proxy/stealth może pokazywać 0 ofert. Działają: OLX, Allegro Lokalnie, Pepper.
- **Pepper** to portal z dealsami; konsole PS5 w przedziale 500–2000 zł pojawiają się tam okazjonalnie.
