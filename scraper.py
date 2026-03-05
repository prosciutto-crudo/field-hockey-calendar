#!/usr/bin/env python3
"""
Field Hockey Calendar Scraper — JUNIOR FC NEGRE
Scrapes individual match pages from fchockey.cat and generates an ICS file.

Strategy: the group results page is dynamically loaded (AngularJS + API auth),
but individual match pages ARE server-side rendered. We scan all match IDs for
the group, filter for our team, and parse each page.

- Matches WITH a time  → timed event (90 min) + warm-up 45 min before
- Matches WITHOUT time → all-day event (no warm-up, labelled "time TBC")
- Address pulled from SSR HTML → Google Maps link in description
- Smart title-case applied to team names and addresses
"""

import math
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from icalendar import Calendar, Event
from urllib.parse import quote

# ── Season config (update each new season) ────────────────────────────────────
FIRST_MATCH_ID      = 62765   # ID of the very first match in the group
MATCHES_PER_JORNADA = 3       # (n_teams choose 2) / n_teams = 7 teams → 3 per jornada
TOTAL_JORNADAS      = 14      # Number of regular-season jornadas
SCAN_BUFFER         = 10      # Extra IDs to scan beyond the last expected match

# ── Team / calendar config ────────────────────────────────────────────────────
TEAM_NAME        = "JUNIOR FC NEGRE"
OUTPUT_FILE      = "hockey_calendar.ics"
CALENDAR_NAME    = "🏑 Junior FC – Alevines Negre"
GAME_DURATION    = 90   # minutes
WARMUP_BEFORE    = 45   # minutes before kickoff

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; calendar-scraper/1.0)"}


# ── Smart title-case ──────────────────────────────────────────────────────────
_ALWAYS_UPPER = {"FC", "HC", "JFC", "ATHC", "CHC", "RC"}
_ALWAYS_LOWER = {"de", "del", "la", "el", "els", "les", "i", "al", "als", "vs"}

def smart_title(text):
    """
    Readable mixed case: keep known abbreviations ALL CAPS, lower Catalan/Spanish
    particles, capitalise everything else.
    e.g. "JUNIOR FC NEGRE"        → "Junior FC Negre"
         "PASSATGE SANT MAMET, 3" → "Passatge Sant Mamet, 3"
         "SANT CUGAT DEL VALLÉS"  → "Sant Cugat del Vallés"
    """
    if not text:
        return text
    result = []
    for i, word in enumerate(text.split()):
        core = word.rstrip(",.-")
        upper, lower = core.upper(), core.lower()
        if upper in _ALWAYS_UPPER:
            result.append(word.upper())
        elif i > 0 and lower in _ALWAYS_LOWER:
            result.append(word.lower())
        else:
            result.append(word[0].upper() + word[1:].lower() if word else word)
    return " ".join(result)


# ── Match page parser ─────────────────────────────────────────────────────────

def fetch_match(match_id):
    """
    Fetch and parse a single match page.
    Returns a dict with match data, or None if our team isn't in this match.

    The SSR page has a consistent text layout (after stripping whitespace):
      line[N-6]: Competition name
      line[N-5]: Home team
      line[N-4]: Score ("-" if not played yet)
      line[N-3]: Away team
      line[N-2]: Venue / club name
      line[N-1]: Full address (street, postal, city in parens)
      line[N]:   Date [time]   e.g. "14/03/2026 09:30" or "21/03/2026"
    """
    url = f"https://www.fchockey.cat/partit/{match_id}"
    resp = requests.get(url, headers=HEADERS, timeout=12)
    resp.raise_for_status()

    if TEAM_NAME not in resp.text:
        return None   # Not our team — skip

    soup = BeautifulSoup(resp.text, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]

    # Find the date line — the anchor for the rest
    date_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^\d{1,2}/\d{2}/\d{4}", line):
            # Make sure it's not a navigation date — confirm team names are nearby
            nearby = " ".join(lines[max(0,i-6):i]).upper()
            if i >= 5 and TEAM_NAME in nearby:
                date_idx = i
                break

    if date_idx is None:
        return None

    home_raw   = lines[date_idx - 5]
    away_raw   = lines[date_idx - 3]
    venue_raw  = lines[date_idx - 2]
    addr_raw   = lines[date_idx - 1]
    date_raw   = lines[date_idx]

    # ── Parse date/time ───────────────────────────────────────────────────────
    dt_parsed = day_parsed = None
    for fmt in ["%d/%m/%Y %H:%M", "%d/%m/%Y"]:
        try:
            parsed = datetime.strptime(date_raw.strip(), fmt)
            if fmt == "%d/%m/%Y %H:%M":
                dt_parsed = parsed
            else:
                day_parsed = parsed.date()
            break
        except ValueError:
            pass

    if not dt_parsed and not day_parsed:
        return None

    # ── Parse address → Google Maps URL ──────────────────────────────────────
    # Format is like: "PASSATGE SANT MAMET, 3-5 08195 BARCELONA (SANT CUGAT DEL VALLÉS)"
    # or:             "Carrer Antic Camí Ral de València, 4 08860 BARCELONA (CASTELLDEFELS)"
    # We want to map-search on the street + postal + city, not the region in parens.
    addr_clean = re.sub(r'\s*\([^)]+\)\s*$', '', addr_raw).strip()  # remove trailing (CITY)
    city_match = re.search(r'\(([^)]+)\)\s*$', addr_raw)
    city_name  = city_match.group(1).strip() if city_match else ""
    address_for_maps = f"{addr_clean}, {city_name}".strip(", ") if city_name else addr_clean

    maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(address_for_maps)}"

    # Display address: title-case the street, keep city
    # Split: "PASSATGE SANT MAMET, 3-5 08195" (street+postal) + city from parens
    street_postal = smart_title(addr_clean)
    city_display  = smart_title(city_name)
    address_display = f"{street_postal}, {city_display}".strip(", ") if city_display else street_postal

    # ── Jornada number (derived from match ID) ────────────────────────────────
    offset  = match_id - FIRST_MATCH_ID   # 0-based offset within the group
    jornada = math.ceil((offset + 1) / MATCHES_PER_JORNADA)

    return {
        "match_id":        match_id,
        "jornada":         jornada,
        "home":            smart_title(home_raw),
        "away":            smart_title(away_raw),
        "venue":           smart_title(venue_raw),
        "address_display": address_display,
        "maps_url":        maps_url,
        "datetime":        dt_parsed,
        "date":            day_parsed,
    }


# ── ICS event builders ────────────────────────────────────────────────────────

def make_event(uid, summary, description, location, start, end):
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", summary)
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("description", description)
    if location:
        ev.add("location", location)
    return ev


def build_calendar(matches):
    cal = Calendar()
    cal.add("prodid", "-//JFC Negre Hockey Scraper//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", CALENDAR_NAME)
    cal.add("x-wr-timezone", "Europe/Madrid")
    cal.add("x-published-ttl", "PT12H")

    for m in matches:
        j       = m["jornada"]
        home    = m["home"]
        away    = m["away"]
        addr    = m["address_display"]
        maps    = m["maps_url"]
        mid     = m["match_id"]

        is_home = TEAM_NAME in m["home"].upper()
        opponent = away if is_home else home
        icon     = "🏠" if is_home else "✈️"

        summary_game   = f"{icon} JFC Negre vs {opponent} (J{j})"
        summary_warmup = "🔥 Calentamiento"

        desc_lines = [f"Jornada {j}", f"{home} vs {away}"]
        if addr:
            desc_lines.append(f"📍 {addr}")
        if maps:
            desc_lines.append(f"🗺️ Cómo llegar: {maps}")
        description = "\n".join(desc_lines)

        uid_base = f"jfcnegre-{mid}-fchockey"

        if m["datetime"]:
            kick = m["datetime"]
            # Game
            cal.add_component(make_event(
                uid=f"{uid_base}-game",
                summary=summary_game,
                description=description,
                location=addr,
                start=kick,
                end=kick + timedelta(minutes=GAME_DURATION),
            ))
            # Warm-up — lean description, just the essentials
            warmup_desc = f"Saque: {kick.strftime('%H:%M')}"
            if addr:
                warmup_desc += f"\n📍 {addr}"
            cal.add_component(make_event(
                uid=f"{uid_base}-warmup",
                summary=summary_warmup,
                description=warmup_desc,
                location=addr,
                start=kick - timedelta(minutes=WARMUP_BEFORE),
                end=kick,
            ))
        else:
            day = m["date"]
            allday_desc = description + "\n\n⏰ Hora de inicio pendiente de confirmación."
            cal.add_component(make_event(
                uid=f"{uid_base}-game",
                summary=f"🏑 JFC Negre vs {opponent} (J{j}) – hora por confirmar",
                description=allday_desc,
                location=addr,
                start=day,
                end=day + timedelta(days=1),
            ))

    return cal


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    total_ids = TOTAL_JORNADAS * MATCHES_PER_JORNADA + SCAN_BUFFER
    scan_range = range(FIRST_MATCH_ID, FIRST_MATCH_ID + total_ids)

    print(f"🔍 Scanning {len(scan_range)} match pages for '{TEAM_NAME}'…")
    print(f"   IDs: {scan_range.start} – {scan_range.stop - 1}\n")

    matches = []
    for match_id in scan_range:
        try:
            m = fetch_match(match_id)
            if m:
                matches.append(m)
                dt_str = m["datetime"].strftime("%a %d %b  %H:%M") if m["datetime"] else str(m["date"]) + " (TBC)"
                icon = "⏱️ " if m["datetime"] else "📅"
                print(f"  {icon} J{m['jornada']:>2} | {dt_str} | {m['home']} vs {m['away']}")
                print(f"        📍 {m['address_display']}")
        except Exception as e:
            print(f"  ⚠️  /partit/{match_id}: {e}")

    print(f"\n✅ Found {len(matches)} matches")
    timed  = sum(1 for m in matches if m["datetime"])
    allday = sum(1 for m in matches if not m["datetime"])
    print(f"   {timed} timed  (→ {timed * 2} calendar events with warm-ups)")
    print(f"   {allday} all-day (→ {allday} events, time TBC)")
    print(f"   {timed * 2 + allday} total events\n")

    cal = build_calendar(matches)
    with open(OUTPUT_FILE, "wb") as f:
        f.write(cal.to_ical())
    print(f"📅 Written: {OUTPUT_FILE}")
