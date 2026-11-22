#!/usr/bin/env python3
"""
BookMyShow Ticket Availability Tracker (Hyderabad)
--------------------------------------------------
Monitors BookMyShow every 15 minutes for:
  - Movies: Avengers: Doomsday, Dune: Part Three (Dune 3)
  - Language: English
  - Cinemas & Screens:
      1. Prasads Multiplex: Hyderabad (PCX screen)
      2. ALLU Cinemas: Kokapet (Dolby Cinema screen)
  - Dates: December 18, 19, and 20, 2026

Phone Notifications:
  Uses ntfy.sh (free, no account required).
  1. Install the "ntfy" app on Android or iOS (https://ntfy.sh).
  2. Subscribe to a unique topic name in the app (e.g., "bms-hyd-alerts-9482").
  3. Set NTFY_TOPIC below to that exact topic name.
  (Optional Telegram bot support is also included below.)

Dependencies:
  pip install requests beautifulsoup4 curl_cffi

Usage:
  Run continuously (checks every 15 minutes):
    python3 bms_ticket_tracker.py

  Run once (useful for cron / Task Scheduler every 15 mins):
    python3 bms_ticket_tracker.py --once
"""

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set

import requests
from bs4 import BeautifulSoup

# Prefer curl_cffi to mimic a real browser TLS fingerprint (helps avoid Cloudflare 403s).
try:
    from curl_cffi import requests as cf_requests
    HTTP_CLIENT = "curl_cffi"
except ImportError:
    cf_requests = None
    HTTP_CLIENT = "requests"


# ==============================================================================
# CONFIGURATION — EDIT THESE BEFORE RUNNING
# ==============================================================================

# 1. Phone Notification (ntfy.sh) — change this to a private, unique topic name!
NTFY_TOPIC = "bms-hyd-tickets-change-me"
NTFY_SERVER = "https://ntfy.sh"

# 2. Optional: Telegram Bot (leave blank if using ntfy.sh)
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

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


def fetch_page(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    if HTTP_CLIENT == "curl_cffi" and cf_requests is not None:
        resp = cf_requests.get(url, headers=headers, impersonate="chrome", timeout=25)
    else:
        resp = requests.get(url, headers=headers, timeout=25)

    resp.raise_for_status()
    return resp.text


def extract_matching_shows(
    html: str, cinema: Dict, date_str: str
) -> List[Dict[str, str]]:
    """
    Parses the BookMyShow venue page HTML for matching movie + language + screen.
    Note: BookMyShow's DOM/JSON structure can change; this checks both structured
    showtime blocks and embedded page text/JSON.
    """
    matches: List[Dict[str, str]] = []
    soup = BeautifulSoup(html, "html.parser")

    # Verify the page actually loaded the requested date (BMS redirects invalid/future
    # dates to today's date if bookings for that date aren't open yet).
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        if date_str not in canonical["href"] and not canonical["href"].endswith(cinema["code"]):
            # If canonical points to a different YYYYMMDD date, this date isn't live yet
            if re.search(r"/202\d{5}$", canonical["href"]) and not canonical["href"].endswith(date_str):
                return []

    # Strategy 1: Inspect movie listing blocks in the DOM
    # BookMyShow venue pages group showtimes by movie container (`li.list` or `div` blocks)
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
            # Extract showtime strings like "07:30 PM" or "11:45 AM"
            times = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", text, flags=re.IGNORECASE)
            # Determine which movie matched
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

    # 1. Send via ntfy.sh
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

    # 2. Send via Telegram (if configured)
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
    """Runs one full check across all cinemas and target dates. Returns True if new shows found."""
    found_new = False

    for date_str in TARGET_DATES:
        for cinema in CINEMAS:
            url = cinema["url_template"].format(date=date_str)
            try:
                html = fetch_page(url)
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
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                log(f"HTTP {status} for {cinema['code']} on {date_str} ({url})")
            except Exception as exc:
                log(f"Error checking {cinema['code']} on {date_str}: {exc}")

            # Polite pause between requests
            time.sleep(2)

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

    log(f"Starting BookMyShow tracker (HTTP client: {HTTP_CLIENT})")
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
