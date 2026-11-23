#!/usr/bin/env python3
"""
BookMyShow Ticket Availability Tracker (Hyderabad)
--------------------------------------------------
Monitors BookMyShow using headless Chromium (Playwright) for:
  - Movies: Avengers: Doomsday, Dune: Part Three (Dune 3)
  - Language: English
  - Cinemas & Screens:
      1. Prasads Multiplex: Hyderabad (PCX screen)
      2. ALLU Cinemas: Kokapet (Dolby Cinema screen)
  - Dates: December 18, 19, and 20, 2026

Phone Notifications:
  Supports Telegram Bot (via TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID env vars)
  and/or ntfy.sh (via NTFY_TOPIC env var).

Dependencies:
  pip install requests beautifulsoup4 playwright
  playwright install chromium

Usage:
  Run continuously (checks every 15 minutes):
    python3 bms_ticket_tracker.py

  Run once (useful for cron / GitHub Actions every 15 mins):
    python3 bms_ticket_tracker.py --once
"""

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# ==============================================================================
# CONFIGURATION — SET VIA ENVIRONMENT VARIABLES / GITHUB SECRETS OR EDIT BELOW
# ==============================================================================

# 1. Phone Notification (ntfy.sh)
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "bms-hyd-tickets-change-me")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# 2. Telegram Bot (recommended: set via GitHub Secrets / env vars)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 3. Check Interval (15 minutes)
CHECK_INTERVAL_SECONDS = 15 * 60

# 4. Target Dates (YYYYMMDD)
TARGET_DATES = ["20261218", "20261219", "20261220"]

# 5. Target Movies & Language (case-insensitive substring match)
TARGET_MOVIE_KEYWORDS = ["avengers", "doomsday", "dune"]
TARGET_LANGUAGE = "english"

# 6. Target Cinemas & Required Screens
CINEMAS = [
    {
        "name": "Prasads Multiplex: Hyderabad",
        "code": "PRHN",
        "url_template": (
            "https://in.bookmyshow.com/cinemas/hyderabad/"
            "prasads-multiplex-hyderabad/buytickets/PRHN/{date}"
        ),
        "screen_keywords": ["pcx"],
    },
    {
        "name": "ALLU Cinemas: Kokapet",
        "code": "ALUC",
        "url_template": (
            "https://in.bookmyshow.com/cinemas/hyderabad/"
            "allu-cinemas-kokapet/buytickets/ALUC/{date}"
        ),
        "screen_keywords": ["dolby cinema", "dolby"],
    },
]

# Stores already-notified show keys so you don't get repeated alerts for the same show
STATE_FILE = Path(__file__).with_name("bms_notified_shows.json")


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def load_notified_shows() -> Set[str]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return set(data)
        except Exception:
            return set()
    return set()


def save_notified_shows(notified: Set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(notified), indent=2), encoding="utf-8")


def extract_matching_shows(
    html: str, cinema: Dict, date_str: str
) -> List[Dict[str, str]]:
    """Parses the rendered BookMyShow venue page HTML for matching movie + language + screen."""
    matches: List[Dict[str, str]] = []
    soup = BeautifulSoup(html, "html.parser")

    # Verify the page actually loaded the requested date (BMS redirects invalid/future
    # dates to today's date if bookings for that date aren't open yet).
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        if date_str not in canonical["href"] and not canonical["href"].endswith(cinema["code"]):
            if re.search(r"/202\d{5}$", canonical["href"]) and not canonical["href"].endswith(date_str):
                return []

    candidate_blocks = soup.select("li.list, div[data-movie-code], section, article")
    if not candidate_blocks:
        candidate_blocks = [soup]

    seen_signatures = set()

    for block in candidate_blocks:
        text = " ".join(block.get_text(" ", strip=True).split())
        if not text or len(text) > 4000:
            continue

        lower_text = text.lower()
        movie_hit = any(kw in lower_text for kw in TARGET_MOVIE_KEYWORDS)
        lang_hit = TARGET_LANGUAGE in lower_text
        screen_hit = any(sk in lower_text for sk in cinema["screen_keywords"])

        if movie_hit and lang_hit and screen_hit:
            times = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", text, flags=re.IGNORECASE)
            matched_movie = (
                "Avengers: Doomsday"
                if ("avengers" in lower_text or "doomsday" in lower_text)
                else "Dune: Part Three"
            )
            times_str = ", ".join(sorted(set(times))) if times else "Showtimes detected"
            signature = f"{cinema['code']}|{date_str}|{matched_movie}|{times_str}"

            if signature not in seen_signatures:
                seen_signatures.add(signature)
                matches.append(
                    {
                        "id": signature,
                        "cinema": cinema["name"],
                        "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
                        "movie": matched_movie,
                        "times": times_str,
                        "url": cinema["url_template"].format(date=date_str),
                    }
                )

    return matches


def send_phone_notification(title: str, message: str, click_url: str) -> None:
    sent = False

    if NTFY_TOPIC and NTFY_TOPIC != "bms-hyd-tickets-change-me":
        try:
            resp = requests.post(
                f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={
                    "Title": title,
                    "Priority": "high",
                    "Tags": "tickets,clapper",
                    "Click": click_url,
                },
                timeout=15,
            )
            resp.raise_for_status()
            log(f"Sent ntfy notification to topic '{NTFY_TOPIC}'.")
            sent = True
        except Exception as exc:
            log(f"Failed to send ntfy notification: {exc}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = requests.post(
                tg_url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": f"🎬 {title}\n\n{message}\n\nBook here: {click_url}",
                },
                timeout=15,
            )
            resp.raise_for_status()
            log("Sent Telegram notification.")
            sent = True
        except Exception as exc:
            log(f"Failed to send Telegram notification: {exc}")

    if not sent:
        log(
            "WARNING: No notification channel configured! "
            "Set NTFY_TOPIC or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID."
        )


def run_check(notified_shows: Set[str]) -> bool:
    """Runs one full check across all cinemas and target dates using headless Chromium."""
    found_new = False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        for date_str in TARGET_DATES:
            for cinema in CINEMAS:
                url = cinema["url_template"].format(date=date_str)
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    status = resp.status if resp else 0
                    # Give Cloudflare JS / client-side hydration a moment to settle
                    page.wait_for_timeout(3500)
                    html = page.content()

                    if status == 403 or "just a moment" in html.lower() or "attention required" in html.lower():
                        log(
                            f"Blocked by Cloudflare (HTTP {status}) for {cinema['code']} "
                            f"on {date_str}. (GitHub Actions datacenter IPs are often blocked; "
                            f"try running locally if this persists.)"
                        )
                        continue

                    matches = extract_matching_shows(html, cinema, date_str)
                    for match in matches:
                        if match["id"] not in notified_shows:
                            found_new = True
                            notified_shows.add(match["id"])
                            title = f"Tickets Open: {match['movie']} ({match['date']})"
                            body = (
                                f"{match['movie']} (English) is available at {match['cinema']} "
                                f"on {match['date']}.\nShowtimes: {match['times']}"
                            )
                            log(f"MATCH FOUND! {body}")
                            send_phone_notification(title, body, match["url"])
                except Exception as exc:
                    log(f"Error checking {cinema['code']} on {date_str}: {exc}")

                time.sleep(2)

        browser.close()

    if found_new:
        save_notified_shows(notified_shows)
    else:
        log("No matching openings found in this check.")

    return found_new


def main() -> None:
    parser = argparse.ArgumentParser(description="BookMyShow Ticket Tracker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (for cron / Task Scheduler).",
    )
    args = parser.parse_args()

    log("Starting BookMyShow tracker (HTTP client: Playwright Chromium)")
    notified_shows = load_notified_shows()

    if args.once:
        run_check(notified_shows)
        return

    while True:
        run_check(notified_shows)
        log(f"Sleeping for {CHECK_INTERVAL_SECONDS // 60} minutes...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
