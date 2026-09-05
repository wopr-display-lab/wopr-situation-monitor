#!/usr/bin/env python3
"""800x480 Raspberry Pi Situation Monitor fed by readsb on port 30003."""

import csv
import io
import json
import math
import socket
import threading
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pygame


WIDTH, HEIGHT = 800, 480
FPS = 30
PAGE_SECONDS = 15
READSB_HOST, READSB_PORT = "127.0.0.1", 30003
# Set these three values for the monitor's installation location.
HOME_LAT, HOME_LON = 35.0000, -97.0000
LOCATION_NAME = "YOUR LOCATION"
CONTACT_TTL = 90
RADAR_RANGE_NM = 50
AIRSPACE_METER_MAX = 20
TRAIL_SECONDS = 120
ARCHIVE_DIR = Path.home() / "SituationMonitor_archive"
SCREENSHOT_REQUEST = ARCHIVE_DIR / "CAPTURE_SCREENSHOT"

BLACK = (4, 3, 1)
AMBER = (245, 156, 34)
BRIGHT_AMBER = (255, 218, 112)
DIM_AMBER = (139, 78, 16)
VERY_DIM = (48, 27, 7)
PANEL_FILL = (13, 8, 2)
MILITARY_RED = (255, 75, 35)

MILITARY_ICAO_RANGES = (
    (0xAE0000, 0xAEFFFF, "US MIL"),
    (0x43C000, 0x43CFFF, "UK MIL"),
)
MILITARY_CALLSIGN_PREFIXES = (
    "REACH", "RCH", "EVAC", "PAT", "CNV", "SAM", "SPAR",
)

pygame.init()
screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.FULLSCREEN | pygame.NOFRAME)
pygame.display.set_caption("SITUATION MONITOR")
pygame.event.set_grab(True)
pygame.mouse.set_visible(False)
pygame.mouse.set_pos((WIDTH - 1, HEIGHT - 1))
try:
    invisible_cursor = pygame.cursors.Cursor(
        (8, 8), (0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
    )
    pygame.mouse.set_cursor(invisible_cursor)
except (pygame.error, TypeError):
    pass
clock = pygame.time.Clock()

FONT_REGULAR_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


def monitor_font(size, bold=False):
    """Use a predictable Pi font instead of an undersized SysFont fallback."""
    font_path = FONT_BOLD_PATH if bold else FONT_REGULAR_PATH
    try:
        return pygame.font.Font(font_path, size)
    except (OSError, pygame.error):
        return pygame.font.SysFont("dejavusansmono", size, bold=bold)


FONT_BIG = monitor_font(36, bold=True)
FONT_MED = monitor_font(28, bold=True)
FONT_SMALL = monitor_font(21)
FONT_TINY = monitor_font(16)
FONT_SPACE = monitor_font(24, bold=True)


def text(message, x, y, font=FONT_SMALL, color=AMBER):
    screen.blit(font.render(str(message), True, color), (x, y))


def line(color, start, end, width=1):
    pygame.draw.line(screen, color, start, end, width)


def panel(rect, corner=10):
    """Draw a recessed WOPR-era instrument bay behind page data."""
    x, y, w, h = rect
    pygame.draw.rect(screen, PANEL_FILL, rect)
    pygame.draw.rect(screen, VERY_DIM, rect, 1)
    # Clipped corners and registration marks make this feel built, not styled.
    line(DIM_AMBER, (x, y), (x + corner, y), 2)
    line(DIM_AMBER, (x, y), (x, y + corner), 2)
    line(DIM_AMBER, (x + w - corner, y + h), (x + w, y + h), 2)
    line(DIM_AMBER, (x + w, y + h - corner), (x + w, y + h), 2)


def header(title, page):
    screen.fill(BLACK)
    for x in range(20, WIDTH - 20, 40):
        line(VERY_DIM, (x, 88), (x, 444))
    for y in range(88, 445, 40):
        line(VERY_DIM, (15, y), (785, y))
    pygame.draw.rect(screen, VERY_DIM, (12, 7, 776, 76), 1)
    pygame.draw.rect(screen, DIM_AMBER, (17, 11, 7, 42))
    text("W.O.P.R. // SITUATION MONITOR", 32, 9, FONT_MED, BRIGHT_AMBER)
    text(f"{page:02d}  {title}", 32, 39, FONT_SMALL, AMBER)
    text("F1 STATUS  F2 AIR  F3 1090  F4 WX+SPACE  F5 INTEL  F6 ORBIT  F7 SEC",
         20, 63, FONT_TINY, DIM_AMBER)
    pygame.draw.circle(screen, BRIGHT_AMBER, (684, 22), 4)
    text("ONLINE", 696, 13, FONT_TINY, BRIGHT_AMBER)
    text(f"SYS-{page:02d}  {int(time.time()) & 0xFFFF:04X}",
         663, 41, FONT_TINY, DIM_AMBER)
    line(AMBER, (15, 84), (785, 84), 2)


def bottom(status):
    pygame.draw.rect(screen, PANEL_FILL, (12, 447, 776, 29))
    line(AMBER, (15, 447), (785, 447), 2)
    text("> " + status[:59], 18, 454, FONT_TINY)
    text(datetime.now().strftime("%H:%M:%S"), 690, 455, FONT_TINY, DIM_AMBER)


def crt_overlay(page_age=99):
    """Brief WOPR terminal acquisition wipe, with no simulated CRT filter."""
    if page_age < 0.21:
        revealed = int(HEIGHT * min(1.0, page_age / 0.21))
        if revealed < HEIGHT:
            pygame.draw.rect(screen, BLACK, (0, revealed, WIDTH, HEIGHT - revealed))
        line(BRIGHT_AMBER, (0, revealed), (WIDTH, revealed), 2)


def distance_bearing(lat, lon):
    p1, p2 = math.radians(HOME_LAT), math.radians(lat)
    dp = math.radians(lat - HOME_LAT)
    dl = math.radians(lon - HOME_LON)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    miles = 3958.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return miles / 1.15078, (math.degrees(math.atan2(y, x)) + 360) % 360


def contacts_in_range():
    contacts = []
    for ac in feed.aircraft.values():
        if "lat" not in ac or "lon" not in ac:
            continue
        dist, bearing = distance_bearing(ac["lat"], ac["lon"])
        if dist <= RADAR_RANGE_NM:
            contacts.append((dist, bearing, ac))
    return contacts


class RadarTrails:
    """Keep a short position history without changing readsb tracking."""

    def __init__(self):
        self.points = {}
        self.next_sample = 0

    def update(self):
        now = time.time()
        if now < self.next_sample:
            return
        self.next_sample = now + 2
        active = set()
        for dist, bearing, ac in contacts_in_range():
            icao = ac["icao"]
            active.add(icao)
            history = self.points.setdefault(icao, deque(maxlen=60))
            history.append((now, dist, bearing))
        cutoff = now - TRAIL_SECONDS
        for icao in list(self.points):
            history = self.points[icao]
            while history and history[0][0] < cutoff:
                history.popleft()
            if not history or (icao not in active and history[-1][0] < cutoff):
                del self.points[icao]


radar_trails = RadarTrails()


def military_identity(ac):
    """Return a conservative public-ADS-B military label and evidence, or None."""
    try:
        address = int(ac.get("icao", ""), 16)
    except (TypeError, ValueError):
        address = -1
    for first, last, label in MILITARY_ICAO_RANGES:
        if first <= address <= last:
            return label, "ICAO BLOCK"
    callsign = "".join(ac.get("callsign", "").upper().split())
    if any(callsign.startswith(prefix) for prefix in MILITARY_CALLSIGN_PREFIXES):
        return "MIL", "CALLSIGN"
    return None


def system_uptime():
    try:
        with open("/proc/uptime", "r", encoding="ascii") as uptime_file:
            seconds = int(float(uptime_file.read().split()[0]))
    except (OSError, ValueError, IndexError):
        seconds = int(time.monotonic())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}D {hours:02d}H {minutes:02d}M" if days else f"{hours:02d}H {minutes:02d}M"


class ReadsbFeed:
    """Non-blocking reader for readsb's BaseStation/SBS output."""

    def __init__(self):
        self.sock = None
        self.buffer = ""
        self.next_connect = 0
        self.connected = False
        self.aircraft = {}
        self.message_times = deque()
        self.total_messages = 0
        self.last_message = 0
        self.activity = deque([0] * 60, maxlen=60)
        self.activity_second = int(time.time())

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.connected = False

    def connect(self):
        now = time.time()
        if self.sock or now < self.next_connect:
            return
        self.next_connect = now + 3
        try:
            sock = socket.create_connection((READSB_HOST, READSB_PORT), timeout=0.15)
            sock.setblocking(False)
            self.sock, self.connected = sock, True
        except OSError:
            self.connected = False

    def roll_activity(self):
        second = int(time.time())
        while self.activity_second < second:
            self.activity.append(0)
            self.activity_second += 1

    def update(self):
        self.roll_activity()
        self.connect()
        if self.sock:
            try:
                while True:
                    chunk = self.sock.recv(16384)
                    if not chunk:
                        self.close()
                        break
                    self.buffer += chunk.decode("ascii", errors="ignore")
            except BlockingIOError:
                pass
            except OSError:
                self.close()
        lines = self.buffer.split("\n")
        self.buffer = lines.pop() if lines else ""
        for raw in lines:
            self.parse(raw.strip())
        cutoff = time.time() - CONTACT_TTL
        self.aircraft = {icao: ac for icao, ac in self.aircraft.items()
                         if ac.get("seen", 0) >= cutoff}
        while self.message_times and self.message_times[0] < time.time() - 60:
            self.message_times.popleft()

    def parse(self, raw):
        fields = raw.split(",")
        if len(fields) < 22 or fields[0] != "MSG":
            return
        now = time.time()
        self.total_messages += 1
        self.last_message = now
        self.message_times.append(now)
        self.activity[-1] += 1
        icao = fields[4].strip().upper()
        if not icao:
            return
        ac = self.aircraft.setdefault(icao, {"icao": icao})
        ac["seen"] = now
        for key, index in (("callsign", 10), ("alt", 11), ("speed", 12),
                           ("track", 13), ("lat", 14), ("lon", 15)):
            value = fields[index].strip()
            if not value:
                continue
            try:
                ac[key] = value if key == "callsign" else float(value)
            except ValueError:
                pass

    @property
    def rate(self):
        return len(self.message_times) / 60.0


feed = ReadsbFeed()


class WeatherFeed:
    """Refresh NWS conditions in the background without pausing the display."""

    def __init__(self):
        self.data = {}
        self.status = "LOADING LIVE WEATHER"
        self.updated = 0
        self.fetching = False
        self.next_fetch = 0
        self.forecast_url = None
        self.station_url = None

    @staticmethod
    def get_json(url):
        request = urllib.request.Request(url, headers={
            "User-Agent": "SituationMonitor/2.1 (Raspberry Pi appliance)",
            "Accept": "application/geo+json, application/json",
        })
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.load(response)

    @staticmethod
    def value(properties, name):
        return (properties.get(name) or {}).get("value")

    def update(self):
        if not self.fetching and time.time() >= self.next_fetch:
            self.fetching = True
            self.next_fetch = time.time() + 600
            threading.Thread(target=self.fetch, daemon=True).start()

    def fetch(self):
        try:
            if not self.forecast_url or not self.station_url:
                point = self.get_json(
                    f"https://api.weather.gov/points/{HOME_LAT:.4f},{HOME_LON:.4f}"
                )["properties"]
                self.forecast_url = point["forecastHourly"]
                stations = self.get_json(point["observationStations"])
                self.station_url = stations["features"][0]["id"]
            obs = self.get_json(self.station_url + "/observations/latest")["properties"]
            periods = self.get_json(self.forecast_url)["properties"]["periods"][:5]
            temp_c = self.value(obs, "temperature")
            dewpoint_c = self.value(obs, "dewpoint")
            wind_item = obs.get("windSpeed") or {}
            wind_value = wind_item.get("value")
            wind_unit = wind_item.get("unitCode", "")
            pressure_pa = self.value(obs, "barometricPressure")
            if pressure_pa is None:
                pressure_pa = self.value(obs, "seaLevelPressure")
            visibility_m = self.value(obs, "visibility")
            humidity = self.value(obs, "relativeHumidity")
            if humidity is None and temp_c is not None and dewpoint_c is not None:
                vapor = math.exp((17.625 * dewpoint_c) / (243.04 + dewpoint_c))
                saturation = math.exp((17.625 * temp_c) / (243.04 + temp_c))
                humidity = max(0, min(100, 100 * vapor / saturation))
            used_model_fallback = False
            if temp_c is None or humidity is None or pressure_pa is None:
                fallback = self.get_json(
                    "https://api.open-meteo.com/v1/forecast"
                    f"?latitude={HOME_LAT:.4f}&longitude={HOME_LON:.4f}"
                    "&current=temperature_2m,relative_humidity_2m,pressure_msl"
                ).get("current", {})
                if temp_c is None:
                    temp_c = fallback.get("temperature_2m")
                if humidity is None:
                    humidity = fallback.get("relative_humidity_2m")
                if pressure_pa is None and fallback.get("pressure_msl") is not None:
                    pressure_pa = fallback["pressure_msl"] * 100
                used_model_fallback = True
            if wind_value is None:
                wind_mph = None
            elif "km_h-1" in wind_unit:
                wind_mph = round(wind_value * 0.621371)
            elif "m_s-1" in wind_unit:
                wind_mph = round(wind_value * 2.23694)
            elif "knot" in wind_unit:
                wind_mph = round(wind_value * 1.15078)
            else:
                wind_mph = round(wind_value)
            optional = self.fetch_optional_weather()
            self.data = {
                "temp": None if temp_c is None else round(temp_c * 9 / 5 + 32),
                "conditions": (obs.get("textDescription") or "NOT REPORTED").upper(),
                "wind_dir": obs.get("windDirection", {}).get("value"),
                "wind": wind_mph,
                "humidity": None if humidity is None else round(humidity),
                "pressure": None if pressure_pa is None else pressure_pa / 3386.389,
                "visibility": None if visibility_m is None else visibility_m / 1609.344,
                "forecast": periods,
                **optional,
            }
            self.updated = time.time()
            self.status = "LIVE NWS + MODEL FALLBACK" if used_model_fallback else "LIVE NWS WEATHER"
        except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError):
            self.status = "NWS OFFLINE - USING LAST DATA" if self.data else "WAITING FOR NWS"
            self.next_fetch = time.time() + 60
        finally:
            self.fetching = False

    def fetch_optional_weather(self):
        result = {"sunrise": None, "sunset": None, "alert": None}
        try:
            sun = self.get_json(
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={HOME_LAT:.4f}&longitude={HOME_LON:.4f}"
                "&daily=sunrise,sunset&timezone=auto&forecast_days=1"
            )["daily"]
            result["sunrise"] = sun["sunrise"][0][11:16]
            result["sunset"] = sun["sunset"][0][11:16]
        except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError):
            pass
        try:
            alerts = self.get_json(
                f"https://api.weather.gov/alerts/active?point={HOME_LAT:.4f},{HOME_LON:.4f}"
            ).get("features", [])
            result["alert"] = (alerts[0].get("properties", {}).get("event")
                               if alerts else "NONE")
        except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError):
            pass
        return result


weather = WeatherFeed()


class IntelligenceFeed:
    """Fetch small RSS and market summaries on a daemon thread."""

    NEWS_FEEDS = (
        ("U.S.", "https://www.cbsnews.com/latest/rss/us"),
        ("TECH", "https://www.cbsnews.com/latest/rss/technology"),
        ("SPACE", "https://www.nasa.gov/rss/dyn/breaking_news.rss"),
    )
    MARKET_SYMBOLS = (
        ("DJIA", "^DJI", "^dji"),
        ("S&P 500", "^GSPC", "^spx"),
        ("NASDAQ", "^IXIC", "^ndq"),
    )

    def __init__(self):
        self.headlines = []
        self.markets = {}
        self.updated = 0
        self.fetching = False
        self.next_fetch = 0
        self.status = "LOADING INTELLIGENCE FEEDS"

    @staticmethod
    def get_bytes(url, timeout=6):
        request = urllib.request.Request(
            url, headers={"User-Agent": "SituationMonitor/2.1 (Raspberry Pi appliance)"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(512000)

    def update(self):
        if not self.fetching and time.time() >= self.next_fetch:
            self.fetching = True
            self.next_fetch = time.time() + 900
            threading.Thread(target=self.fetch, daemon=True).start()

    def fetch_market_change(self, yahoo_symbol, stooq_symbol, start, end):
        """Return the change between the two latest closes, with a fallback."""
        try:
            symbol = urllib.parse.quote(yahoo_symbol, safe="")
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                   "?range=5d&interval=1d")
            payload = json.loads(self.get_bytes(url).decode("utf-8"))
            result = payload["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            closes = [float(value) for value in closes if value is not None]
            if len(closes) >= 2:
                return (closes[-1] / closes[-2] - 1) * 100
        except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            pass

        try:
            query = urllib.parse.urlencode({
                "s": stooq_symbol, "d1": start, "d2": end, "i": "d",
            })
            rows = list(csv.DictReader(io.StringIO(
                self.get_bytes("https://stooq.com/q/d/l/?" + query).decode(
                    "utf-8", errors="replace"
                )
            )))
            closes = [float(row["Close"]) for row in rows if row.get("Close")]
            if len(closes) >= 2:
                return (closes[-1] / closes[-2] - 1) * 100
        except (OSError, ValueError, KeyError, csv.Error):
            pass
        return None

    def fetch(self):
        try:
            new_headlines = []
            new_markets = {}
            for category, url in self.NEWS_FEEDS:
                try:
                    root = ET.fromstring(self.get_bytes(url))
                    for item in root.findall(".//item")[:2]:
                        title = " ".join((item.findtext("title") or "").split())
                        if title:
                            new_headlines.append((category, title))
                except (OSError, ValueError, ET.ParseError):
                    continue
            start = (datetime.now() - timedelta(days=12)).strftime("%Y%m%d")
            end = datetime.now().strftime("%Y%m%d")
            for label, yahoo_symbol, stooq_symbol in self.MARKET_SYMBOLS:
                change = self.fetch_market_change(
                    yahoo_symbol, stooq_symbol, start, end
                )
                if change is not None:
                    new_markets[label] = change
            if new_headlines:
                self.headlines = new_headlines[:5]
            if new_markets:
                self.markets = new_markets
            if new_headlines or new_markets:
                self.updated = time.time()
                self.status = "LIVE PUBLIC INTELLIGENCE FEEDS"
            else:
                self.status = ("FEEDS OFFLINE - USING LAST DATA" if self.updated
                               else "INTELLIGENCE FEEDS UNAVAILABLE")
                self.next_fetch = time.time() + 60
        finally:
            self.fetching = False


intelligence = IntelligenceFeed()


class SECScoreFeed:
    """Fetch the SEC football scoreboard without blocking the display."""

    SEC_TEAMS = {
        "ALA", "ARK", "AUB", "FLA", "UGA", "UK", "LSU", "MISS",
        "MSST", "MIZ", "OU", "SC", "TENN", "TEX", "TA&M", "VAN",
    }
    SEC_LOCATIONS = {
        "Alabama", "Arkansas", "Auburn", "Florida", "Georgia", "Kentucky",
        "LSU", "Mississippi State", "Missouri", "Oklahoma", "Ole Miss",
        "South Carolina", "Tennessee", "Texas", "Texas A&M", "Vanderbilt",
    }

    def __init__(self):
        self.games = []
        self.updated = 0
        self.fetching = False
        self.next_fetch = 0
        self.status = "LOADING SEC SCOREBOARD"

    def update(self):
        if not self.fetching and time.time() >= self.next_fetch:
            self.fetching = True
            threading.Thread(target=self.fetch, daemon=True).start()

    @staticmethod
    def get_scoreboard(url):
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux armv7l) SituationMonitor/2.2",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(request, timeout=7) as response:
            return json.load(response)

    @property
    def relevant_today(self):
        return any(game["state"] == "in" or game["today"] for game in self.games)

    @property
    def rotation_active(self):
        now = datetime.now()
        football_season = now.month in (8, 9, 10, 11, 12, 1)
        football_weekend = now.weekday() in (3, 4, 5)
        return self.relevant_today or (football_season and football_weekend)

    def fetch(self):
        try:
            today = datetime.now().date()
            query = urllib.parse.urlencode({
                "groups": 80,
                "limit": 200,
                "dates": f"{today:%Y%m%d}",
            })
            payload = self.get_scoreboard(
                "https://site.api.espn.com/apis/site/v2/sports/football/"
                "college-football/scoreboard?" + query
            )
            games = []
            for event in payload.get("events", []):
                competition = (event.get("competitions") or [{}])[0]
                teams = {}
                for competitor in competition.get("competitors", []):
                    side = competitor.get("homeAway", "")
                    team = competitor.get("team", {})
                    teams[side] = {
                        "name": team.get("abbreviation") or team.get("shortDisplayName") or "TBD",
                        "location": team.get("location") or team.get("shortDisplayName") or "",
                        "score": competitor.get("score", "0"),
                    }
                if "home" not in teams or "away" not in teams:
                    continue
                abbreviations = {teams["home"]["name"], teams["away"]["name"]}
                locations = {teams["home"]["location"], teams["away"]["location"]}
                if not (abbreviations & self.SEC_TEAMS or locations & self.SEC_LOCATIONS):
                    continue
                start = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone()
                status = event.get("status", {}).get("type", {})
                state = status.get("state", "pre")
                detail = status.get("shortDetail") or status.get("detail") or "SCHEDULED"
                broadcasts = competition.get("broadcasts") or []
                network = ", ".join(broadcasts[0].get("names", [])) if broadcasts else ""
                games.append({
                    "away": teams["away"],
                    "home": teams["home"],
                    "start": start,
                    "state": state,
                    "detail": detail.upper(),
                    "network": network.upper(),
                    "today": start.date() == today,
                })
            priority = {"in": 0, "pre": 1, "post": 2}
            games.sort(key=lambda game: (priority.get(game["state"], 3), game["start"]))
            self.games = games
            self.updated = time.time()
            self.status = "LIVE ESPN / SEC TEAMS" if games else "NO SEC GAMES FOUND TODAY"
            self.next_fetch = time.time() + (30 if any(
                game["state"] == "in" for game in games
            ) else 300)
        except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            self.status = "SCORE FEED OFFLINE - USING LAST DATA" if self.games else "SEC SCORE FEED UNAVAILABLE"
            self.next_fetch = time.time() + 60
        finally:
            self.fetching = False


sec_scores = SECScoreFeed()


class SatelliteFeed:
    """Predict selected satellite passes in a background thread."""

    SATELLITES = (
        ("ISS", 25544),
        ("TIANGONG", 48274),
        ("HUBBLE", 20580),
    )
    ORBIT_CACHE = ARCHIVE_DIR / "satellite_orbits.json"

    def __init__(self):
        self.data = []
        self.status = "LOADING ORBITAL DATA"
        self.updated = 0
        self.fetching = False
        self.next_fetch = 0

    def update(self):
        if not self.fetching and time.time() >= self.next_fetch:
            self.fetching = True
            self.next_fetch = time.time() + 300
            threading.Thread(target=self.fetch, daemon=True).start()

    def get_elements(self):
        """Fetch standard TLEs, falling back to SatNOGS and the last good copy."""
        found = {}
        sources = set()
        for _, catalog_number in self.SATELLITES:
            # This is the simple TLE path used by the original working page.
            try:
                url = ("https://celestrak.org/NORAD/elements/gp.php?CATNR="
                       f"{catalog_number}&FORMAT=TLE")
                lines = [line.strip() for line in
                         IntelligenceFeed.get_bytes(url, timeout=10)
                         .decode("utf-8", "replace").splitlines()
                         if line.strip()]
                line1 = next(line for line in lines if line.startswith("1 "))
                line2 = next(line for line in lines if line.startswith("2 "))
                found[catalog_number] = {"line1": line1, "line2": line2}
                sources.add("CELESTRAK")
            except (OSError, ValueError, TypeError, StopIteration,
                    json.JSONDecodeError):
                pass

            # HST is also published in CelesTrak's Science catalog.  Keep this
            # separate route because some networks intermittently return an
            # empty response for its individual catalog-number query.
            if catalog_number == 20580 and catalog_number not in found:
                try:
                    url = ("https://celestrak.org/NORAD/elements/gp.php?"
                           "GROUP=SCIENCE&FORMAT=TLE")
                    lines = [line.strip() for line in
                             IntelligenceFeed.get_bytes(url, timeout=10)
                             .decode("utf-8", "replace").splitlines()
                             if line.strip()]
                    for index, line1 in enumerate(lines):
                        if (line1.startswith("1 20580") and
                                index + 1 < len(lines) and
                                lines[index + 1].startswith("2 20580")):
                            found[catalog_number] = {
                                "line1": line1, "line2": lines[index + 1]
                            }
                            sources.add("CELESTRAK")
                            break
                except (OSError, ValueError, TypeError):
                    pass

            # AMSAT independently republishes current NASA element sets and is
            # reachable on installations where CelesTrak is filtered upstream.
            if catalog_number == 20580 and catalog_number not in found:
                try:
                    url = "https://www.amsat.org/tle/current/nasa.all"
                    lines = [line.strip() for line in
                             IntelligenceFeed.get_bytes(url, timeout=10)
                             .decode("utf-8", "replace").splitlines()
                             if line.strip()]
                    for index, line1 in enumerate(lines):
                        if (line1.startswith("1 20580") and
                                index + 1 < len(lines) and
                                lines[index + 1].startswith("2 20580")):
                            found[catalog_number] = {
                                "line1": line1, "line2": lines[index + 1]
                            }
                            sources.add("AMSAT")
                            break
                except (OSError, ValueError, TypeError):
                    pass

            # Independent no-key fallback.  SatNOGS may return either a list or
            # a paginated object depending on its API version.
            if catalog_number not in found:
                try:
                    url = ("https://db.satnogs.org/api/tle/?norad_cat_id="
                           f"{catalog_number}")
                    payload = json.loads(IntelligenceFeed.get_bytes(
                        url, timeout=10).decode("utf-8"))
                    records = (payload.get("results", [])
                               if isinstance(payload, dict) else payload)
                    record = records[0]
                    line1 = record.get("tle1") or record.get("TLE_LINE1")
                    line2 = record.get("tle2") or record.get("TLE_LINE2")
                    if not (line1 and line2):
                        raise ValueError("TLE lines missing")
                    found[catalog_number] = {"line1": line1, "line2": line2}
                    sources.add("SATNOGS")
                except (OSError, ValueError, KeyError, IndexError, TypeError,
                        json.JSONDecodeError):
                    pass
        if found:
            try:
                ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                self.ORBIT_CACHE.write_text(json.dumps(found), encoding="utf-8")
            except OSError:
                pass
            return found, "+".join(sorted(sources))
        try:
            cached = json.loads(self.ORBIT_CACHE.read_text(encoding="utf-8"))
            cached = {int(key): value for key, value in cached.items()}
            # Ignore cache files created by the short-lived OMM implementation.
            cached = {key: value for key, value in cached.items()
                      if value.get("line1") and value.get("line2")}
            return cached, "CACHED"
        except (OSError, ValueError, TypeError, AttributeError,
                json.JSONDecodeError):
            return {}, ""

    def fetch(self):
        try:
            from skyfield.api import EarthSatellite, load, wgs84
        except ImportError:
            self.status = "SPACE DATA NEEDS: sudo apt install python3-skyfield"
            self.fetching = False
            self.next_fetch = time.time() + 3600
            return
        try:
            ts = load.timescale(builtin=True)
            observer = wgs84.latlon(HOME_LAT, HOME_LON)
            now = ts.now()
            end = ts.from_datetime(datetime.now(timezone.utc) + timedelta(hours=36))
            results = []
            elements, element_source = self.get_elements()
            for short_name, catalog_number in self.SATELLITES:
                element_record = elements.get(catalog_number)
                if not element_record:
                    continue
                try:
                    satellite = EarthSatellite(
                        element_record["line1"], element_record["line2"],
                        short_name, ts
                    )
                    altitude, azimuth, _ = (satellite - observer).at(now).altaz()
                    times, events = satellite.find_events(
                        observer, now, end, altitude_degrees=10.0
                    )
                except (KeyError, ValueError, TypeError):
                    continue
                next_time, max_elevation, pass_track = None, None, []
                event_list = list(zip(times, events))
                for event_index, (event_time, event) in enumerate(event_list):
                    if event == 1:
                        pass_altitude, _, _ = (satellite - observer).at(event_time).altaz()
                        next_time = event_time.utc_datetime().astimezone()
                        max_elevation = pass_altitude.degrees
                        pass_start = now
                        pass_end = event_time
                        for prior_time, prior_event in reversed(event_list[:event_index]):
                            if prior_event == 0:
                                pass_start = prior_time
                                break
                        for later_time, later_event in event_list[event_index + 1:]:
                            if later_event == 2:
                                pass_end = later_time
                                break
                        start_dt = pass_start.utc_datetime()
                        end_dt = pass_end.utc_datetime()
                        duration = max(1.0, (end_dt - start_dt).total_seconds())
                        for step in range(25):
                            sample_dt = start_dt + timedelta(seconds=duration * step / 24)
                            sample_time = ts.from_datetime(sample_dt)
                            sample_alt, sample_az, _ = (
                                satellite - observer
                            ).at(sample_time).altaz()
                            if sample_alt.degrees >= 0:
                                pass_track.append(
                                    (sample_alt.degrees, sample_az.degrees)
                                )
                        break
                results.append({
                    "name": short_name,
                    "alt": altitude.degrees,
                    "az": azimuth.degrees,
                    "next": next_time,
                    "max": max_elevation,
                    "track": pass_track,
                })
            if results:
                self.data = results
                self.updated = time.time()
                prefix = "CACHED" if element_source == "CACHED" else "LIVE"
                source = ("" if element_source == "CACHED"
                          else f" {element_source}")
                self.status = f"{prefix}{source} ORBITS / 10 DEG PASSES"
            else:
                self.status = ("ORBITAL DATA UNAVAILABLE - USING LAST DATA"
                               if self.data else "NO ORBITAL ELEMENTS RECEIVED")
                self.next_fetch = time.time() + 60
        except Exception as error:
            self.status = ("ORBITAL FEED OFFLINE - USING LAST DATA" if self.data
                           else f"ORBIT ERROR: {type(error).__name__.upper()}")
            self.next_fetch = time.time() + 60
        finally:
            self.fetching = False


satellites = SatelliteFeed()


class SpaceWeatherFeed:
    """Small, non-blocking NOAA space-weather snapshot."""

    def __init__(self):
        self.data = {}
        self.status = "ACQUIRING NOAA SPACE WEATHER"
        self.fetching = False
        self.next_fetch = 0

    def update(self):
        if not self.fetching and time.time() >= self.next_fetch:
            self.fetching = True
            self.next_fetch = time.time() + 900
            threading.Thread(target=self.fetch, daemon=True).start()

    def fetch(self):
        new_data = {}
        try:
            kp = json.loads(IntelligenceFeed.get_bytes(
                "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json",
                timeout=8).decode("utf-8"))
            if kp:
                new_data["kp"] = float(kp[-1].get("kp_index"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            flux = json.loads(IntelligenceFeed.get_bytes(
                "https://services.swpc.noaa.gov/json/f107_cm_flux.json",
                timeout=8).decode("utf-8"))
            if flux:
                value = flux[-1].get("flux") or flux[-1].get("f107")
                new_data["flux"] = float(value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        if new_data:
            self.data.update(new_data)
            self.status = "LIVE NOAA SPACE WEATHER"
        else:
            self.status = ("NOAA OFFLINE - USING LAST DATA" if self.data
                           else "NOAA SPACE WEATHER UNAVAILABLE")
            self.next_fetch = time.time() + 60
        self.fetching = False


space_weather = SpaceWeatherFeed()


class DailyArchive:
    """Save a compact rolling summary without logging individual messages."""

    def __init__(self):
        self.day = datetime.now().strftime("%Y-%m-%d")
        self.aircraft = set()
        self.military = set()
        self.peak_rate = 0.0
        self.closest = None
        self.next_save = 0
        self.saving = False
        self.status = "ARCHIVE INITIALIZING"
        self.load_today()

    @property
    def path(self):
        return ARCHIVE_DIR / f"{self.day}.json"

    def load_today(self):
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.aircraft = set(saved.get("unique_aircraft", []))
            self.military = set(saved.get("military_aircraft", []))
            self.peak_rate = float(saved.get("peak_message_rate", 0))
            self.closest = saved.get("closest_contact")
            self.status = "DAILY ARCHIVE ACTIVE"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def update(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.aircraft, self.military = set(), set()
            self.peak_rate, self.closest = 0.0, None
            self.load_today()
        for ac in feed.aircraft.values():
            self.aircraft.add(ac["icao"])
            if military_identity(ac):
                self.military.add(ac["icao"])
        self.peak_rate = max(self.peak_rate, feed.rate)
        for distance, _, ac in contacts_in_range():
            if self.closest is None or distance < self.closest.get("distance_nm", 9999):
                self.closest = {
                    "icao": ac["icao"],
                    "callsign": ac.get("callsign", "").strip(),
                    "distance_nm": round(distance, 2),
                    "time": datetime.now().isoformat(timespec="seconds"),
                }
        if time.time() >= self.next_save and not self.saving:
            self.next_save = time.time() + 60
            payload = {
                "date": self.day,
                "updated": datetime.now().isoformat(timespec="seconds"),
                "unique_aircraft": sorted(self.aircraft),
                "military_aircraft": sorted(self.military),
                "peak_message_rate": round(self.peak_rate, 2),
                "closest_contact": self.closest,
                "total_messages_since_start": feed.total_messages,
            }
            self.saving = True
            threading.Thread(target=self.save, args=(payload,), daemon=True).start()

    def save(self, payload):
        try:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self.path)
            self.status = "DAILY ARCHIVE ACTIVE"
        except OSError:
            self.status = "DAILY ARCHIVE WRITE FAILED"
        finally:
            self.saving = False


archive = DailyArchive()


def shown(value, suffix="", decimals=0):
    if value is None:
        return "--"
    return f"{value:.{decimals}f}{suffix}"


def compass(degrees):
    if degrees is None:
        return "--"
    points = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return points[int((degrees + 22.5) // 45) % 8]


def wrap_words(message, max_chars):
    words, lines, current = message.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def moon_state():
    """Approximate phase from a known new moon; no network or ephemeris needed."""
    epoch = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    age = ((datetime.now(timezone.utc) - epoch).total_seconds() / 86400.0
           % 29.53058867)
    fraction = age / 29.53058867
    illumination = (1 - math.cos(2 * math.pi * fraction)) / 2
    names = ("NEW MOON", "WAXING CRESCENT", "FIRST QUARTER", "WAXING GIBBOUS",
             "FULL MOON", "WANING GIBBOUS", "LAST QUARTER", "WANING CRESCENT")
    return age, fraction, illumination, names[int((fraction * 8) + .5) % 8]


def draw_status():
    header("STRATEGIC STATUS / SYSTEM READINESS", 1)
    panel((18, 96, 764, 123))
    panel((18, 228, 764, 92))
    panel((18, 329, 764, 105))
    online = feed.connected and feed.last_message > time.time() - 10
    positioned = contacts_in_range()
    count = len(positioned)
    activity = "LOW" if count < 5 else "MODERATE" if count < 12 else "HIGH"
    text("NOMINAL" if online else "STANDBY", 25, 105, FONT_BIG, BRIGHT_AMBER)
    text("LEVEL 1", 650, 110, FONT_MED)
    text("LOCAL TIME", 25, 160, FONT_SMALL, DIM_AMBER)
    text(datetime.now().strftime("%H:%M:%S"), 25, 180, FONT_BIG)
    text("DATE", 300, 160, FONT_SMALL, DIM_AMBER)
    text(datetime.now().strftime("%m/%d/%Y"), 300, 180, FONT_MED)
    text("SYSTEM UPTIME", 560, 160, FONT_SMALL, DIM_AMBER)
    text(system_uptime(), 560, 180, FONT_MED)
    text("AIRSPACE ACTIVITY / 50 NM", 25, 235, FONT_SMALL, DIM_AMBER)
    text(activity, 650, 235, FONT_SMALL, BRIGHT_AMBER)
    text(f"{count} CONTACTS", 25, 278, FONT_MED)
    bx, by, bw, bh = 250, 275, 500, 28
    pygame.draw.rect(screen, AMBER, (bx, by, bw, bh), 2)
    fill = int((bw - 10) * min(count, AIRSPACE_METER_MAX) / AIRSPACE_METER_MAX)
    if fill:
        pygame.draw.rect(screen, AMBER, (bx + 5, by + 5, fill, bh - 10))
    text(f"SCALE 0-{AIRSPACE_METER_MAX}", 650, 306, FONT_TINY, DIM_AMBER)
    text("RADAR ONLINE" if online else "WAITING FOR READSB", 25, 335)
    text(f"1090 MHz MESSAGE RATE  {feed.rate:.1f}/SEC", 25, 365)
    text("WEATHER SYSTEM READY" if weather.updated else "WEATHER SYSTEM LOADING", 25, 395)
    bottom("ALL SYSTEMS NOMINAL" if online else "CONNECTING TO READSB :30003")


def draw_radar():
    header("AIRBORNE TRACKING / ADS-B", 2)
    panel((18, 96, 443, 338))
    panel((475, 96, 307, 338))
    cx, cy, radius = 300, 265, 155
    for r in (40, 80, 120, 155):
        pygame.draw.circle(screen, DIM_AMBER, (cx, cy), r, 1)
    line(DIM_AMBER, (cx - radius, cy), (cx + radius, cy))
    line(DIM_AMBER, (cx, cy - radius), (cx, cy + radius))
    angle = time.time() * 1.3
    line(BRIGHT_AMBER, (cx, cy),
         (cx + math.sin(angle) * radius, cy - math.cos(angle) * radius), 2)
    positioned = contacts_in_range()
    military_count = 0
    label_icaos = {
        ac["icao"] for _, _, ac in sorted(positioned, key=lambda item: item[0])[:8]
    }
    label_icaos.update(
        ac["icao"] for _, _, ac in positioned if military_identity(ac)
    )
    for icao, history in radar_trails.points.items():
        plotted = []
        for _, dist, bearing in history:
            rr, aa = dist / RADAR_RANGE_NM * radius, math.radians(bearing)
            plotted.append((int(cx + math.sin(aa) * rr),
                            int(cy - math.cos(aa) * rr)))
        if len(plotted) > 1:
            trail_color = MILITARY_RED if any(
                ac.get("icao") == icao and military_identity(ac)
                for ac in feed.aircraft.values()
            ) else DIM_AMBER
            pygame.draw.lines(screen, trail_color, False, plotted, 1)
    for dist, bearing, ac in positioned:
        rr, aa = dist / RADAR_RANGE_NM * radius, math.radians(bearing)
        x, y = cx + math.sin(aa) * rr, cy - math.cos(aa) * rr
        identity = military_identity(ac)
        label = ac.get("callsign", ac["icao"]).strip()
        if identity:
            military_count += 1
            px, py = int(x), int(y)
            pygame.draw.polygon(screen, MILITARY_RED,
                                ((px, py - 6), (px - 6, py + 5), (px + 6, py + 5)), 2)
            if ac["icao"] in label_icaos:
                text("MIL " + label, px + 8, py - 8, FONT_TINY, MILITARY_RED)
        else:
            pygame.draw.circle(screen, BRIGHT_AMBER, (int(x), int(y)), 4)
            if ac["icao"] in label_icaos:
                text(label, int(x) + 6, int(y) - 7, FONT_TINY, BRIGHT_AMBER)
    text("1090 MHz / ADS-B", 490, 105, FONT_MED)
    text("READSB FEED", 490, 145, FONT_SMALL, DIM_AMBER)
    text("ONLINE" if feed.connected else "RECONNECTING", 490, 165, FONT_MED)
    text("CONTACTS", 490, 215, FONT_SMALL, DIM_AMBER)
    text(len(positioned), 490, 235, FONT_BIG)
    text(f"MIL CONFIRMED  {military_count}", 620, 245, FONT_TINY,
         MILITARY_RED if military_count else DIM_AMBER)
    text("RANGE", 490, 295, FONT_SMALL, DIM_AMBER)
    text(f"{RADAR_RANGE_NM} NM", 490, 315, FONT_MED)
    text("MESSAGES / SEC", 490, 360, FONT_SMALL, DIM_AMBER)
    text(f"{feed.rate:.1f}", 490, 380, FONT_MED)
    if positioned:
        nearest_dist, _, nearest_ac = min(positioned, key=lambda item: item[0])
        nearest_name = nearest_ac.get("callsign", "").strip() or nearest_ac["icao"]
        text(f"NEAREST  {nearest_name}  {nearest_dist:.1f} NM",
             490, 414, FONT_TINY,
             MILITARY_RED if military_identity(nearest_ac) else BRIGHT_AMBER)
    recent = feed.last_message > time.time() - 10
    if not feed.connected:
        radar_status = "WAITING FOR READSB"
    elif not recent:
        radar_status = "READSB CONNECTED - NO RECENT MESSAGES"
    elif not positioned:
        radar_status = "MESSAGES RECEIVED - NO POSITION CONTACTS IN RANGE"
    else:
        radar_status = "LIVE READSB AIRSPACE DATA"
    bottom(radar_status)


def draw_activity():
    header("SIGNAL ACTIVITY / 1090 MHz", 3)
    panel((18, 92, 764, 62))
    panel((18, 158, 764, 166))
    panel((18, 333, 764, 101))
    text("READSB", 20, 94, FONT_MED, BRIGHT_AMBER)
    text("SBS PORT 30003", 150, 99, FONT_SMALL)
    text("● RX ACTIVE" if feed.last_message > time.time() - 2 else "○ RX WAIT",
         650, 99, FONT_SMALL, BRIGHT_AMBER if feed.connected else DIM_AMBER)
    text(f"RATE {feed.rate:.1f}/SEC", 25, 128, FONT_SMALL)
    text(f"TOTAL {feed.total_messages:,}", 245, 128, FONT_SMALL)
    text(f"TRACKED {len(feed.aircraft)}", 500, 128, FONT_SMALL)
    text("ID", 25, 163, FONT_TINY, DIM_AMBER)
    text("FLIGHT / ICAO", 70, 163, FONT_TINY, DIM_AMBER)
    text("ALT FT", 230, 163, FONT_TINY, DIM_AMBER)
    text("SPD", 330, 163, FONT_TINY, DIM_AMBER)
    text("HDG", 410, 163, FONT_TINY, DIM_AMBER)
    text("DIST", 490, 163, FONT_TINY, DIM_AMBER)
    text("HEARD", 610, 163, FONT_TINY, DIM_AMBER)
    line(DIM_AMBER, (25, 180), (775, 180))
    contacts = []
    for ac in feed.aircraft.values():
        distance = None
        if "lat" in ac and "lon" in ac:
            distance, _ = distance_bearing(ac["lat"], ac["lon"])
        contacts.append((distance if distance is not None else 9999, ac))
    contacts.sort(key=lambda item: (item[0], -item[1].get("seen", 0)))
    for row, (distance, ac) in enumerate(contacts[:5]):
        y = 192 + row * 27
        label = ac.get("callsign", "").strip() or ac["icao"]
        identity = military_identity(ac)
        text("MIL" if identity else "CIV", 25, y, FONT_TINY,
             MILITARY_RED if identity else DIM_AMBER)
        text(label, 70, y, FONT_SMALL, MILITARY_RED if identity else BRIGHT_AMBER)
        text(shown(ac.get("alt"), "", 0), 230, y)
        text(shown(ac.get("speed"), "", 0), 330, y)
        text(shown(ac.get("track"), "°", 0), 410, y)
        text("--" if distance == 9999 else f"{distance:.1f}", 490, y)
        text(f"{int(time.time() - ac.get('seen', time.time()))} SEC", 610, y)
    if not contacts:
        text("NO AIRCRAFT MESSAGES CURRENTLY RECEIVED", 25, 220, FONT_MED, DIM_AMBER)
    gx, gy, gw, gh = 25, 340, 750, 80
    pygame.draw.rect(screen, VERY_DIM, (gx, gy, gw, gh), 1)
    peak = max(1, max(feed.activity))
    bar_w = gw / len(feed.activity)
    points = []
    for i, count in enumerate(feed.activity):
        h = int((count / peak) * (gh - 20))
        points.append((int(gx + i * bar_w), gy + gh - h))
    if len(points) > 1:
        pygame.draw.lines(screen, BRIGHT_AMBER, False, points, 3)
        pygame.draw.lines(screen, DIM_AMBER, False,
                          [(x, min(gy + gh, y + 4)) for x, y in points], 1)
    text("MESSAGE ACTIVITY: 60 SECONDS AGO", gx, 424, FONT_TINY, DIM_AMBER)
    text("NOW", 745, 424, FONT_TINY, DIM_AMBER)
    bottom("LIVE READSB MESSAGE ACTIVITY" if feed.connected else "RECONNECTING TO READSB")


def draw_weather():
    header("ATMOSPHERIC ANALYSIS / LOCAL", 4)
    panel((18, 96, 300, 278))
    panel((323, 145, 220, 229))
    panel((550, 145, 232, 229))
    panel((18, 382, 764, 52))
    data = weather.data
    text(LOCATION_NAME[:18].upper(), 25, 105, FONT_BIG, BRIGHT_AMBER)
    text("CURRENT CONDITIONS", 25, 160, FONT_SMALL, DIM_AMBER)
    text(shown(data.get("temp"), " F"), 25, 185, FONT_BIG)
    text(data.get("conditions", "LOADING...")[:18], 25, 230, FONT_MED)
    text("WIND", 330, 160, FONT_SMALL, DIM_AMBER)
    text(f"{compass(data.get('wind_dir'))} {shown(data.get('wind'), ' MPH')}", 330, 185, FONT_MED)
    text("HUMIDITY", 330, 235, FONT_SMALL, DIM_AMBER)
    text(shown(data.get("humidity"), " %"), 330, 260, FONT_MED)
    text("PRESSURE", 330, 305, FONT_SMALL, DIM_AMBER)
    text(shown(data.get("pressure"), " IN", 2), 330, 330, FONT_MED)
    text("VISIBILITY", 560, 160, FONT_SMALL, DIM_AMBER)
    text(shown(data.get("visibility"), " MI", 1), 560, 185, FONT_MED)
    text("SUNRISE / SUNSET", 560, 235, FONT_SMALL, DIM_AMBER)
    text(f"{data.get('sunrise') or '--:--'} / {data.get('sunset') or '--:--'}", 560, 260, FONT_SMALL)
    text("SEVERE ALERT", 560, 305, FONT_SMALL, DIM_AMBER)
    alert = data.get("alert")
    text((alert or "CHECKING...")[:24], 560, 330, FONT_SMALL,
         MILITARY_RED if alert and alert != "NONE" else AMBER)
    _, _, illumination, phase_name = moon_state()
    kp = space_weather.data.get("kp")
    flux = space_weather.data.get("flux")
    text("LUNAR", 28, 387, FONT_TINY, DIM_AMBER)
    text(f"{phase_name}  {illumination * 100:.0f}%", 28, 406, FONT_SMALL)
    text("GEOMAGNETIC", 350, 387, FONT_TINY, DIM_AMBER)
    text("KP --" if kp is None else f"KP {kp:.1f}", 350, 406, FONT_SMALL,
         MILITARY_RED if kp is not None and kp >= 5 else AMBER)
    text("SOLAR FLUX", 535, 387, FONT_TINY, DIM_AMBER)
    text("-- SFU" if flux is None else f"{flux:.1f} SFU",
         535, 406, FONT_SMALL)
    bottom(weather.status + " / " + space_weather.status)


def draw_intelligence():
    header("STRATEGIC INTELLIGENCE / DATA LINK", 5)
    panel((18, 96, 764, 281))
    panel((18, 382, 764, 52))
    headlines = intelligence.headlines
    if not headlines:
        text("AWAITING CURRENT HEADLINES...", 25, 120, FONT_MED, DIM_AMBER)
    for row, (category, headline) in enumerate(headlines[:3]):
        y = 105 + row * 94
        text(f"{row + 1:02d}  {category}", 25, y, FONT_SMALL, DIM_AMBER)
        wrapped = wrap_words(headline, 44)
        text(wrapped[0][:44], 25, y + 25, FONT_MED, BRIGHT_AMBER)
        if len(wrapped) > 1:
            text(wrapped[1][:44], 25, y + 53, FONT_MED)
        line(VERY_DIM, (25, y + 84), (775, y + 84))
    text("MARKETS / PREVIOUS CLOSE", 25, 389, FONT_SMALL, DIM_AMBER)
    x_positions = (25, 275, 540)
    for x, label in zip(x_positions, ("DJIA", "S&P 500", "NASDAQ")):
        change = intelligence.markets.get(label)
        value = "--" if change is None else f"{change:+.2f}%"
        direction = "--" if change is None else "UP" if change > 0 else "DOWN" if change < 0 else "FLAT"
        text(f"{label} {direction} {value}", x, 416, FONT_SMALL,
             BRIGHT_AMBER if change is not None and change >= 0 else AMBER)
    status = intelligence.status
    if intelligence.updated and time.time() - intelligence.updated > 3600:
        status = "STALE DATA - LAST SUCCESS " + datetime.fromtimestamp(intelligence.updated).strftime("%H:%M")
    bottom(status)


def draw_space():
    header("ORBITAL SURVEILLANCE / SPACE TRACK", 6)
    panel((18, 91, 430, 343))
    panel((454, 96, 328, 292))
    panel((454, 395, 328, 39))
    cx, cy, radius = 245, 270, 155

    def sky_point(altitude, azimuth):
        distance = (90 - max(0, min(90, altitude))) / 90 * radius
        angle = math.radians(azimuth)
        return (int(cx + math.sin(angle) * distance),
                int(cy - math.cos(angle) * distance))

    pygame.draw.circle(screen, AMBER, (cx, cy), radius, 2)
    pygame.draw.circle(screen, DIM_AMBER, (cx, cy), int(radius * 2 / 3), 1)
    pygame.draw.circle(screen, DIM_AMBER, (cx, cy), int(radius / 3), 1)
    line(DIM_AMBER, (cx - radius, cy), (cx + radius, cy))
    line(DIM_AMBER, (cx, cy - radius), (cx, cy + radius))
    text("N", cx - 7, cy - radius + 5, FONT_SPACE, BRIGHT_AMBER)
    text("S", cx - 7, cy + radius + 2, FONT_SPACE, BRIGHT_AMBER)
    text("W", cx - radius - 28, cy - 12, FONT_SPACE, BRIGHT_AMBER)
    text("E", cx + radius + 8, cy - 12, FONT_SPACE, BRIGHT_AMBER)
    text("OKLAHOMA SKY / HORIZON", 92, 94, FONT_SMALL, DIM_AMBER)
    text("OK", cx - 15, cy - 13, FONT_SMALL, DIM_AMBER)

    if not satellites.data:
        status_lines = wrap_words(satellites.status, 22)
        text(status_lines[0], 455, 145, FONT_MED, DIM_AMBER)
        if len(status_lines) > 1:
            text(status_lines[1], 455, 180, FONT_MED, DIM_AMBER)
        if len(status_lines) > 2:
            text(status_lines[2], 455, 215, FONT_MED, DIM_AMBER)
    for row, item in enumerate(satellites.data[:3]):
        y = 112 + row * 92
        above = item["alt"] > 0
        object_color = (BRIGHT_AMBER, AMBER, DIM_AMBER)[row]
        track = [sky_point(alt, az) for alt, az in item.get("track", [])]
        if len(track) > 1:
            pygame.draw.lines(screen, object_color, False, track, 2)
        if above:
            px, py = sky_point(item["alt"], item["az"])
            pygame.draw.circle(screen, object_color, (px, py), 7, 2)
            text(str(row + 1), px + 10, py - 12, FONT_SPACE, object_color)

        text(f"{row + 1}  {item['name']}", 455, y, FONT_MED, object_color)
        text("VISIBLE NOW" if above else "BELOW HORIZON", 465, y + 30,
             FONT_SPACE, BRIGHT_AMBER if above else DIM_AMBER)
        next_pass = item.get("next")
        if next_pass:
            minutes = max(0, int((next_pass - datetime.now().astimezone()).total_seconds() / 60))
            if minutes == 0:
                timing = "PASS IN <1 MIN"
            elif minutes >= 120:
                timing = f"PASS IN {minutes // 60}H {minutes % 60:02d}M"
            else:
                timing = f"PASS IN {minutes} MIN"
            text(timing, 465, y + 58, FONT_SPACE)
        else:
            text("NO PASS NEXT 36 HR", 465, y + 58, FONT_SPACE, DIM_AMBER)
        line(VERY_DIM, (465, y + 86), (775, y + 86))
    text(f"TODAY {len(archive.aircraft)} AC / {len(archive.military)} MIL",
         465, 404, FONT_TINY, DIM_AMBER)
    space_state = satellites.status
    archive_state = "ARCHIVE ACTIVE" if archive.status == "DAILY ARCHIVE ACTIVE" else archive.status
    bottom(space_state + "  |  " + archive_state)


def draw_sec_scoreboard():
    header("REGIONAL CONFLICT / SEC FOOTBALL", 7)
    panel((18, 96, 764, 322))
    games = sec_scores.games[:3]
    if not games:
        text("NO SEC GAMES SCHEDULED", 25, 135, FONT_MED, DIM_AMBER)
        text("CHECKING THE NEXT SEVEN DAYS", 25, 180, FONT_SMALL, DIM_AMBER)
    for row, game in enumerate(games):
        y = 105 + row * 108
        live_color = BRIGHT_AMBER if game["state"] == "in" else AMBER
        away_score = game["away"]["score"] if game["state"] != "pre" else "--"
        home_score = game["home"]["score"] if game["state"] != "pre" else "--"
        text(game["away"]["name"], 25, y, FONT_MED, live_color)
        text(away_score, 310, y - 3, FONT_BIG, live_color)
        text(game["home"]["name"], 25, y + 38, FONT_MED, live_color)
        text(home_score, 310, y + 35, FONT_BIG, live_color)

        if game["state"] == "pre":
            status = game["start"].strftime("%a %I:%M %p").upper()
        else:
            status = game["detail"]
        text(status[:22], 430, y + 3, FONT_MED,
             BRIGHT_AMBER if game["state"] == "in" else AMBER)
        if game["network"]:
            text(game["network"][:25], 430, y + 43, FONT_SMALL, DIM_AMBER)
        line(VERY_DIM, (25, y + 91), (775, y + 91))
    rotation_note = "SEC FOOTBALL ROTATION ACTIVE" if sec_scores.rotation_active else "F7 MANUAL / ROTATION STANDBY"
    text(rotation_note, 25, 424, FONT_TINY, DIM_AMBER)
    bottom(sec_scores.status)


def draw_attract():
    header("AUTONOMOUS PATTERN ANALYSIS", 0)
    panel((18, 100, 764, 334))
    elapsed = time.monotonic() - attract_started
    messages = (
        "ESTABLISHING STRATEGIC DATA LINKS",
        "CORRELATING AIR / ORBITAL TRACKS",
        "EVALUATING ATMOSPHERIC CONDITIONS",
        "SEARCHING FOR PATTERNS",
        "NO IMMEDIATE THREATS DETECTED",
    )
    for row, message in enumerate(messages):
        color = BRIGHT_AMBER if elapsed > row * .8 else DIM_AMBER
        prefix = ">" if elapsed > row * .8 else "_"
        text(f"{prefix} {message}", 55, 130 + row * 52, FONT_MED, color)
    text(f"TRACKS {len(contacts_in_range()):02d}   RX {feed.rate:05.1f}   "
         f"KP {space_weather.data.get('kp', 0):.1f}",
         55, 405, FONT_SMALL, DIM_AMBER)
    bottom("WOPR AUTOMATED ANALYSIS IN PROGRESS")


page = 1
page_changed_at = time.monotonic()
attract_started = -99.0
attract_until = 0.0
next_page_at = time.monotonic() + PAGE_SECONDS
screenshot_requested = False
running = True

try:
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_F12:
                    screenshot_requested = True
                elif event.key in (pygame.K_F1, pygame.K_F2, pygame.K_F3,
                                   pygame.K_F4, pygame.K_F5, pygame.K_F6,
                                   pygame.K_F7):
                    page = {pygame.K_F1: 1, pygame.K_F2: 2, pygame.K_F3: 3,
                            pygame.K_F4: 4, pygame.K_F5: 5,
                            pygame.K_F6: 6, pygame.K_F7: 7}[event.key]
                    attract_until = 0
                    page_changed_at = time.monotonic()
                    next_page_at = time.monotonic() + PAGE_SECONDS
        if time.monotonic() >= next_page_at:
            rotation_pages = [1, 2, 3, 4, 5, 6]
            if sec_scores.rotation_active:
                rotation_pages.append(7)
            try:
                current_index = rotation_pages.index(page)
            except ValueError:
                current_index = -1
            next_index = (current_index + 1) % len(rotation_pages)
            page = rotation_pages[next_index]
            if next_index == 0:
                attract_started = time.monotonic()
                attract_until = attract_started + 6
            page_changed_at = time.monotonic()
            next_page_at = time.monotonic() + PAGE_SECONDS
        feed.update()
        weather.update()
        intelligence.update()
        radar_trails.update()
        satellites.update()
        space_weather.update()
        archive.update()
        sec_scores.update()
        if SCREENSHOT_REQUEST.exists():
            screenshot_requested = True
        if time.monotonic() < attract_until:
            draw_attract()
        else:
            (draw_status, draw_radar, draw_activity, draw_weather,
             draw_intelligence, draw_space, draw_sec_scoreboard)[page - 1]()
        crt_overlay(time.monotonic() - page_changed_at)
        if screenshot_requested:
            try:
                screenshot_dir = ARCHIVE_DIR / "screenshots"
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = screenshot_dir / datetime.now().strftime(
                    "situation-monitor-%Y%m%d-%H%M%S.png"
                )
                pygame.image.save(screen, str(screenshot_path))
            except (OSError, pygame.error):
                pass
            try:
                SCREENSHOT_REQUEST.unlink(missing_ok=True)
            except OSError:
                pass
            screenshot_requested = False
        pygame.display.flip()
        clock.tick(FPS)
finally:
    feed.close()
    pygame.mouse.set_visible(True)
    pygame.quit()
