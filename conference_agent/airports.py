"""Airports near each conference's location, and whether the location is a venue.

The table's Airports column lists the commercial airports within
:data:`AIRPORT_RADIUS_MILES` road miles of the displayed location (the upcoming
edition's, else the prior one's), nearest first, or the single nearest airport
when none is that close. It is derived from the location, never
entered by hand or researched by discovery:

1. The location is geocoded with OpenStreetMap's Nominatim service (free, no
   key; requests are cached in ``data/geocode_cache.json`` and spaced one second
   apart, per its usage policy). For a venue that is the venue; for a city it is
   the city's center (the point a map search for the city shows). The same lookup
   says whether the location is a specific place (a convention center, hotel,
   campus, ...) rather than a city or region; the table links a specific place
   to Google Maps.
2. The candidates are the airports within :data:`AIRPORT_RADIUS_MILES`
   straight-line miles in the OurAirports dataset (public domain, cached in
   ``data/airports.csv`` and re-downloaded monthly) that have scheduled
   passenger service and an IATA code, which leaves out private fields,
   heliports, and seaplane bases.
3. Each candidate's road distance comes from the public OSRM routing service
   (OpenStreetMap roads, no key; cached in ``data/drive_cache.json``), less the
   airside roads OSRM ends a drive with (see :class:`_Router`). An airport with
   no road route (across water) keeps its straight-line miles. Airports more
   than :data:`AIRPORT_RADIUS_MILES` road miles away are dropped, unless none is
   closer, in which case the nearest one is kept (looked for beyond the radius
   when there is no candidate at all). Driving times are not shown: with
   traffic they vary too much to be useful.
4. The airports a traveler would normally fly into are kept as **main**; the
   rest are marked **minor** (see :func:`mark_minor`). Main airports come from
   the busiest tier that has any nearby: those with at least
   :data:`HUB_AIRPORT_PASSENGERS` passengers a year, else at least
   :data:`MAJOR_AIRPORT_PASSENGERS` (or OurAirports' large class when Wikidata,
   cached in ``data/airport_traffic.json``, has no count), else any. Within that
   tier, an airport much farther by road than the nearest one is minor too (LGA
   and JFK, about 30 miles from Cold Spring Harbor, are main; Newark, about 45,
   is minor), unless it is a hub with far more traffic than the main ones
   (Incheon for Seoul). Every list has at least one main airport. The table
   hides minor airports unless its "Include all airports" box is checked.

The stored form is one ``CODE | Name | 28 mi`` line per airport, with
`` | minor`` appended for a minor one (see :func:`format_airports`), so the
column stays readable in the database, the CSV export, and the search.
:func:`refresh_airports` fills it for every row whose location changed since
its airports were found; ``database`` clears the stored airports whenever the
location changes, so a stale list is never shown.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from conference_agent.config import DEFAULT_DATABASE_URL

AIRPORT_RADIUS_MILES = 100
# Most entries a list may hold (a dense metro area has about 15).
MAX_AIRPORTS = 30
# Annual passengers of a hub airport: the main airports are drawn from these
# when any is nearby.
HUB_AIRPORT_PASSENGERS = 10_000_000
# Annual passengers of a commercial airport worth listing by default; the main
# airports are drawn from these when no hub is nearby.
MAJOR_AIRPORT_PASSENGERS = 500_000
# How many of the nearest airports beyond the radius are measured by road when
# no airport is within it (the nearest of them is listed).
FALLBACK_CANDIDATES = 5
# An airport of the main tier is still main when it is no farther than the
# nearest one times this ratio, or plus this many miles (whichever is more),
# so two airports both close to a venue (Midway and O'Hare) are kept.
MAIN_DISTANCE_RATIO = 1.5
MAIN_SLACK_MILES = 10
# A hub outside that limit is still main when it handles at least this many
# times the passengers of the busiest main airport and is no farther than this
# many miles (Incheon for Seoul, Malpensa for Milan, Heathrow for Hinxton).
BUSIER_HUB_RATIO = 2
BUSIER_HUB_MAX_MILES = 75

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRPORTS_MAX_AGE_DAYS = 30
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/"
# Trailing steps of a drive slower than this (mph) are airside roads.
_AIRSIDE_MPH = 12
# An airport whose nearest road is farther than this (meters) has no route.
_MAX_SNAP_METERS = 3000
_OSRM_INTERVAL_S = 1.1
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
TRAFFIC_MAX_AGE_DAYS = 90
# Passenger counts older than this many years are ignored (a closed route's
# old peak says little about service today).
_TRAFFIC_YEARS = 10
# Nominatim requires an identifying User-Agent and at most one request a second.
USER_AGENT = "conference-agent/1.0 (+https://conferenceagent.vercel.app)"
_NOMINATIM_INTERVAL_S = 1.1

# OurAirports types that can have scheduled passenger service and a runway.
_AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}
# A geocoded area wider than this (diagonal, miles) -- a state, country, or
# continent -- has no meaningful "within 100 miles", so it is not used. A city
# is used whatever its extent (Tokyo's boundary takes in islands 600 miles off).
_MAX_AREA_MILES = 150
_CITY_TYPES = {"city", "town", "village", "municipality", "hamlet", "suburb"}
# Nominatim result categories that are a city, region, or boundary rather than
# a specific place.
_AREA_CATEGORIES = {"place", "boundary"}
# Locations that name no place.
_NO_PLACE = re.compile(r"\b(tba|tbd|to be (announced|determined)|virtual|online)\b", re.I)

_EARTH_RADIUS_MILES = 3958.8


def _data_dir() -> Path:
    return Path(os.environ.get("CONFERENCE_DATA_DIR", "data"))


@dataclass(frozen=True)
class Airport:
    """One airport near a location."""

    code: str  # IATA code
    name: str
    miles: int  # road distance from the location (straight-line when no route)
    minor: bool = False  # hidden by default (see mark_minor)


@dataclass(frozen=True)
class Place:
    """A geocoded location."""

    lat: float
    lon: float
    venue: bool  # a specific place (building, venue, campus), not a city or region


# --- The stored text form ----------------------------------------------------


def format_airports(airports: Iterable[Airport]) -> Optional[str]:
    """The stored form: one ``CODE | Name | N mi[ | minor]`` line per airport;
    None if empty."""
    lines = [f"{a.code} | {a.name} | {a.miles} mi" + (" | minor" if a.minor else "") for a in airports]
    return "\n".join(lines) or None


def parse_airports(text: Optional[str]) -> List[Airport]:
    """Read the stored form back into :class:`Airport` entries (unreadable lines
    are skipped)."""
    airports = []
    for line in str(text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or not parts[1]:
            continue
        miles = re.fullmatch(r"(\d+)(?: mi)?", parts[2])  # bare miles: the former form
        if not miles:
            continue
        # A driving time ("46 min") and the "small" marker are former fields.
        rest = [p for p in parts[3:] if not re.fullmatch(r"\d+ min", p)]
        minor = bool(rest) and rest[0] in ("minor", "small")
        airports.append(Airport(parts[0], parts[1], int(miles.group(1)), minor=minor))
    return airports


# --- Geocoding ---------------------------------------------------------------


def single_place(location: Optional[str]) -> Optional[str]:
    """The location as one geocodable place, or None.

    None for a blank value, a placeholder ("TBA", "Virtual"), or several places
    in one value ("Milan, Italy (2027); Stockholm, Sweden (2026)", "Sydney,
    Australia / Paris, France"), since no single airport list fits those.
    """
    text = " ".join(str(location or "").split())
    if not text or _NO_PLACE.search(text) or ";" in text:
        return None
    segments = text.split(" / ")
    # "Van Andel Institute / Grand Valley State University, Grand Rapids, MI" is
    # one city; "Sydney, Australia / Paris, France" is two.
    if any("," in s for s in segments[:-1]):
        return None
    return text


def _queries(location: str) -> List[tuple[str, bool]]:
    """Geocoding queries to try, most specific first, each with whether a match
    on it can show the location is a specific place.

    A parenthetical is tried folded in (a venue: "San Diego, CA (San Diego
    Convention Center)") and dropped; then leading comma-separated parts are
    dropped one at a time, falling back toward the city.
    """
    queries: List[tuple[str, bool]] = []
    paren = re.search(r"\(([^)]*)\)", location)
    bare = " ".join(re.sub(r"\([^)]*\)", " ", location).split()).strip(" ,")
    if paren and paren.group(1).strip():
        queries.append((f"{paren.group(1).strip()}, {bare}", True))
    queries.append((bare, True))
    parts = [p.strip() for p in bare.split(",") if p.strip()]
    for i in range(1, len(parts)):
        queries.append((", ".join(parts[i:]), False))
    seen: set[str] = set()
    return [(q, v) for q, v in queries if q and not (q.lower() in seen or seen.add(q.lower()))]


class _Geocoder:
    """Nominatim lookups with a persistent cache and its one-per-second limit."""

    def __init__(self, cache_path: Optional[Path] = None):
        self.path = cache_path or _data_dir() / "geocode_cache.json"
        try:
            self.cache = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self._last = 0.0

    def search(self, query: str, settlement: bool = False) -> Optional[dict]:
        """The best Nominatim result for ``query`` (cached), or None.

        ``settlement`` limits the search to cities, towns, and villages.
        """
        key = ("settlement:" if settlement else "") + query.lower()
        cached = self.cache.get(key, False)
        # Entries cached before the name and type were kept are fetched again.
        if cached is not False and (cached is None or "addresstype" in cached):
            return cached
        wait = self._last + _NOMINATIM_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        params = {"q": query, "format": "jsonv2", "limit": 1, "accept-language": "en"}
        if settlement:
            params["featureType"] = "settlement"
        url = NOMINATIM_URL + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:  # nosec B310 - fixed https URL
                results = json.load(resp)
        finally:
            self._last = time.monotonic()
        result = None
        if results:
            r = results[0]
            result = {
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "category": r.get("category", ""),
                "addresstype": r.get("addresstype", ""),
                "name": r.get("name", ""),
                "bbox": [float(x) for x in r.get("boundingbox", [])],
            }
        self.cache[key] = result
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")
        return result


def _area_miles(bbox: List[float]) -> float:
    """Diagonal of a ``[south, north, west, east]`` bounding box, in miles."""
    if len(bbox) != 4:
        return 0.0
    south, north, west, east = bbox
    return distance_miles(south, west, north, east)


def geocode(location: Optional[str], geocoder: Optional[_Geocoder] = None) -> Optional[Place]:
    """Geocode a location; None when it names no single place or is not found.

    Raises ``OSError`` when the geocoding service cannot be reached, so a caller
    can retry later rather than record "not found".
    """
    place = single_place(location)
    if place is None:
        return None
    geocoder = geocoder or _Geocoder()
    for query, can_be_venue in _queries(place):
        result = geocoder.search(query)
        if result is None:
            continue
        if _area_miles(result["bbox"]) > _MAX_AREA_MILES and result.get("addresstype") not in _CITY_TYPES:
            continue
        venue = can_be_venue and result["category"] not in _AREA_CATEGORIES
        if venue and _names_a_settlement(query, geocoder):
            venue = False
        return Place(lat=result["lat"], lon=result["lon"], venue=venue)
    return None


def _simple(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _names_a_settlement(query: str, geocoder) -> bool:
    """Whether the query's first part is itself a town or city.

    A place named like a station or district ("Hengqin, China" matches Hengqin
    railway station) is a settlement, not a venue; a venue's name ("McCormick
    Place, Chicago, IL") finds a settlement of another name.
    """
    first = query.split(",")[0]
    result = geocoder.search(query, settlement=True)
    return bool(result) and _simple(result.get("name", "")) == _simple(first)


# --- Airports ----------------------------------------------------------------


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


_AIRPORTS: Optional[list] = None


def _airport_table(path: Optional[Path] = None) -> list:
    """Commercial airports as ``(code, name, lat, lon, large)``, downloading the
    dataset when the cached copy is missing or more than a month old. ``large``
    is OurAirports' own size class, the fallback when no passenger count is
    known."""
    global _AIRPORTS
    if _AIRPORTS is not None and path is None:
        return _AIRPORTS
    path = path or _data_dir() / "airports.csv"
    stale = not path.exists() or time.time() - path.stat().st_mtime > AIRPORTS_MAX_AGE_DAYS * 86400
    if stale:
        request = urllib.request.Request(AIRPORTS_URL, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:  # nosec B310 - fixed https URL
                body = resp.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        except OSError:
            if not path.exists():
                raise  # no copy at all; an old copy is still usable
    table = []
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            code = (r.get("iata_code") or "").strip().upper()
            if r.get("scheduled_service") != "yes" or r.get("type") not in _AIRPORT_TYPES:
                continue
            if not re.fullmatch(r"[A-Z0-9]{3}", code):
                continue
            try:
                table.append((
                    code, r["name"].strip(), float(r["latitude_deg"]), float(r["longitude_deg"]),
                    r["type"] == "large_airport",
                ))
            except (KeyError, ValueError):
                continue
    _AIRPORTS = table
    return table


class _Traffic:
    """Annual passenger counts by IATA code, from Wikidata, cached on disk.

    Codes not yet cached are fetched; the whole cache is refreshed once it is
    older than :data:`TRAFFIC_MAX_AGE_DAYS`. A code Wikidata has no count for is
    cached as None.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _data_dir() / "airport_traffic.json"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        fresh = time.time() - data.get("fetched", 0) <= TRAFFIC_MAX_AGE_DAYS * 86400
        self.counts: dict = data.get("passengers", {}) if fresh else {}
        self.fetched = data.get("fetched", 0) if fresh else 0

    def passengers(self, codes: Iterable[str]) -> dict:
        """``{code: annual passengers or None}`` for the codes (fetching any
        missing). Raises ``OSError`` when Wikidata cannot be reached."""
        codes = sorted(set(codes))
        missing = [c for c in codes if c not in self.counts]
        for i in range(0, len(missing), 200):
            self.counts.update(_fetch_passengers(missing[i:i + 200]))
        if missing:
            self.fetched = self.fetched or time.time()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"fetched": self.fetched, "passengers": self.counts}, indent=1, sort_keys=True),
                encoding="utf-8",
            )
        return {c: self.counts.get(c) for c in codes}


def _fetch_passengers(codes: List[str]) -> dict:
    """The largest annual passenger count (Wikidata P3872) from the last
    :data:`_TRAFFIC_YEARS` years for each IATA code (P238); None when none."""
    values = " ".join(f'"{c}"' for c in codes)
    query = f"""
        SELECT ?iata ?count ?when WHERE {{
          VALUES ?iata {{ {values} }}
          ?airport wdt:P238 ?iata .
          OPTIONAL {{
            ?airport p:P3872 ?statement .
            ?statement ps:P3872 ?count ; wikibase:rank ?rank .
            FILTER(?rank != wikibase:DeprecatedRank)
            OPTIONAL {{ ?statement pq:P585 ?when }}
          }}
        }}"""
    request = urllib.request.Request(
        WIKIDATA_SPARQL_URL,
        data=urllib.parse.urlencode({"query": query, "format": "json"}).encode(),
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(request, timeout=120) as resp:  # nosec B310 - fixed https URL
        bindings = json.load(resp)["results"]["bindings"]
    oldest = date.today().year - _TRAFFIC_YEARS
    out: dict = {c: None for c in codes}
    for b in bindings:
        code = b["iata"]["value"]
        if "count" not in b:
            continue
        year = int(b["when"]["value"][:4]) if "when" in b else None
        if year is not None and year < oldest:
            continue
        try:
            count = int(float(b["count"]["value"]))
        except ValueError:
            continue
        out[code] = max(count, out[code] or 0)
    return out


def airport_tier(passengers: Optional[int], large: bool) -> int:
    """0 for a hub (at least :data:`HUB_AIRPORT_PASSENGERS` a year), 1 for a
    major airport (at least :data:`MAJOR_AIRPORT_PASSENGERS`, or, with no count
    known, a large airport in OurAirports), else 2."""
    if passengers is not None:
        if passengers >= HUB_AIRPORT_PASSENGERS:
            return 0
        return 1 if passengers >= MAJOR_AIRPORT_PASSENGERS else 2
    return 1 if large else 2


def mark_minor(
    airports: Iterable[Airport], tiers: dict, passengers: dict, routed: Optional[set] = None
) -> set:
    """The codes of the minor airports in a list.

    ``tiers`` maps each code to :func:`airport_tier`, and ``passengers`` to its
    annual count (or None). ``routed`` holds the codes with a road route (by
    default, all); when any has one, an airport without is minor. The main
    airports are those of the best (lowest) tier present that are no farther
    than the nearest of that tier times :data:`MAIN_DISTANCE_RATIO` (or plus
    :data:`MAIN_SLACK_MILES`, whichever is more), plus any hub within
    :data:`BUSIER_HUB_MAX_MILES` with at least :data:`BUSIER_HUB_RATIO` times
    the passengers of the busiest of those. Every other airport is minor, so a
    non-empty list has a main airport.
    """
    airports = list(airports)
    pool = [a for a in airports if routed is None or a.code in routed] or airports
    if not pool:
        return set()
    best = min(tiers[a.code] for a in pool)
    nearest = min(a.miles for a in pool if tiers[a.code] == best)
    limit = max(nearest * MAIN_DISTANCE_RATIO, nearest + MAIN_SLACK_MILES)
    main = {a.code for a in pool if tiers[a.code] == best and a.miles <= limit}
    busiest = max((passengers.get(c) or 0 for c in main), default=0)
    main |= {
        a.code for a in pool
        if tiers[a.code] == 0 and a.miles <= BUSIER_HUB_MAX_MILES
        and (passengers.get(a.code) or 0) >= BUSIER_HUB_RATIO * busiest
    }
    return {a.code for a in airports} - main


class _Router:
    """Road distances from OSRM, cached on disk.

    OurAirports places an airport at the middle of its airfield, which OSRM
    snaps to an airside service road; the route then ends with miles of
    9-mph cargo and apron roads (about 3 miles at O'Hare). That slow tail is
    measured once per airport (:meth:`_tail`, from the first location that
    needs it) and subtracted from every drive to the airport.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _data_dir() / "drive_cache.json"
        try:
            self.cache = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self.cache.setdefault("table", {})
        self.cache.setdefault("tail", {})
        self._last = 0.0

    def routes(self, lat: float, lon: float, airports: dict) -> dict:
        """``{code: road miles or None}`` from the point to each airport
        in ``airports`` (``{code: (lat, lon)}``); None for no road route.
        Raises ``OSError`` when the service cannot be reached."""
        key = f"{lat:.5f},{lon:.5f}"
        table = self.cache["table"].setdefault(key, {})
        tails = self.cache["tail"]
        missing = [c for c in airports if c not in table]
        changed = bool(missing)
        if missing:
            table.update(self._table(lat, lon, {c: airports[c] for c in missing}))
        for code in airports:
            if table[code] and code not in tails:
                tails[code] = self._tail(lat, lon, airports[code])
                changed = True
        if changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")
        out = {}
        for code in airports:
            if not table[code]:
                out[code] = None
                continue
            seconds, meters = table[code]
            tail_s, tail_m = tails[code]
            if tail_s < seconds:
                seconds, meters = seconds - tail_s, meters - tail_m
            out[code] = int(round(meters / 1609.344))
        return out

    def _get(self, url: str) -> dict:
        wait = self._last + _OSRM_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:  # nosec B310 - fixed https URL
                body = json.load(resp)
        except ValueError as exc:  # an error page instead of JSON
            raise OSError(f"OSRM: unreadable response ({exc})") from exc
        finally:
            self._last = time.monotonic()
        if body.get("code") not in ("Ok", "NoRoute"):
            raise OSError(f"OSRM: {body.get('code')} {body.get('message', '')}".strip())
        return body

    def _table(self, lat: float, lon: float, airports: dict) -> dict:
        """Raw ``[seconds, meters]`` (or None) to each airport."""
        codes = list(airports)
        points = ";".join(f"{x:.6f},{y:.6f}" for y, x in [(lat, lon)] + [airports[c] for c in codes])
        body = self._get(OSRM_TABLE_URL + points + "?sources=0&annotations=duration,distance")
        out = {}
        for i, code in enumerate(codes, start=1):
            seconds, meters = body["durations"][0][i], body["distances"][0][i]
            snap = body["destinations"][i].get("distance") or 0
            out[code] = None if seconds is None or meters is None or snap > _MAX_SNAP_METERS else [seconds, meters]
        return out

    def _tail(self, lat: float, lon: float, airport: tuple) -> list:
        """``[seconds, meters]`` of the slow airside roads ending the drive to
        an airport: the trailing steps slower than :data:`_AIRSIDE_MPH`, unless
        the whole drive is that slow."""
        points = f"{lon:.6f},{lat:.6f};{airport[1]:.6f},{airport[0]:.6f}"
        body = self._get(OSRM_ROUTE_URL + points + "?overview=false&steps=true")
        if body.get("code") != "Ok":
            return [0, 0]
        steps = [s for leg in body["routes"][0]["legs"] for s in leg["steps"] if s["duration"] > 0]
        seconds = meters = 0.0
        for step in reversed(steps):
            if step["distance"] / step["duration"] * 2.23694 >= _AIRSIDE_MPH:
                return [seconds, meters]
            seconds, meters = seconds + step["duration"], meters + step["distance"]
        return [0, 0]


def nearby_airports(
    lat: float,
    lon: float,
    radius: float = AIRPORT_RADIUS_MILES,
    table: Optional[list] = None,
    traffic: Optional[dict] = None,
    router=None,
) -> List[Airport]:
    """Commercial airports within ``radius`` road miles of a point, nearest
    first; when none is, the single nearest airport (searched beyond the radius
    when none is within it in a straight line). An airport with no road route
    is measured in a straight line.

    ``traffic`` maps IATA codes to annual passengers, looked up by default
    (:class:`_Traffic`); ``router`` gives the road distances (:class:`_Router`).
    """
    table = table if table is not None else _airport_table()
    router = router or _Router()
    by_distance: dict[str, tuple] = {}
    for code, name, alat, alon, large in table:
        miles = distance_miles(lat, lon, alat, alon)
        if code not in by_distance or miles < by_distance[code][1]:
            by_distance[code] = (name, miles, large, alat, alon)
    nearest = sorted(by_distance.items(), key=lambda kv: (kv[1][1], kv[0]))
    near = dict(kv for kv in nearest if kv[1][1] <= radius)
    if not near:
        near = dict(nearest[:FALLBACK_CANDIDATES])
    near = dict(list(near.items())[:MAX_AIRPORTS])
    if not near:
        return []
    roads = router.routes(lat, lon, {c: (v[3], v[4]) for c, v in near.items()})
    measured = sorted(
        (Airport(code, name, roads[code] if roads.get(code) is not None else int(round(miles)))
         for code, (name, miles, _, _, _) in near.items()),
        key=lambda a: (a.miles, a.code),
    )
    airports = [a for a in measured if a.miles <= radius] or measured[:1]
    if traffic is None:
        traffic = _Traffic().passengers(a.code for a in airports)
    tiers = {a.code: airport_tier(traffic.get(a.code), near[a.code][2]) for a in airports}
    routed = {c for c, miles in roads.items() if miles is not None}
    minor = mark_minor(airports, tiers, traffic, routed)
    return [replace(a, minor=a.code in minor) for a in airports]


# --- Filling the database ----------------------------------------------------


def refresh_airports(
    db_url: str = DEFAULT_DATABASE_URL,
    ids: Optional[Iterable[str]] = None,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Look up airports (and the venue flag) for rows whose location changed.

    A row is looked up when it has a location its airports were not found for
    (a new or changed location). ``force`` looks up every row with a location
    again; ``ids`` limits the run to those series. A location that names no
    single place, or is not found, is recorded with no airports. A row whose
    lookup fails to reach a service is left as it was and retried on the next
    run. Returns ``{"updated": n, "not_found": n, "failed": n}``.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from conference_agent.database import ConferenceRow, displayed_location, get_engine

    counts = {"updated": 0, "not_found": 0, "failed": 0}
    geocoder = _Geocoder()
    wanted = set(ids) if ids is not None else None
    with Session(get_engine(db_url)) as session:
        for row in session.scalars(select(ConferenceRow).order_by(ConferenceRow.id)):
            if wanted is not None and row.id not in wanted:
                continue
            location = displayed_location(row)
            if not location or not (force or row.airports_location != location):
                continue
            try:
                place = geocode(location, geocoder)
                airports = nearby_airports(place.lat, place.lon) if place else []
            except OSError as exc:
                counts["failed"] += 1
                log(f"{row.id}: lookup failed for {location!r} ({exc}); will retry next run")
                continue
            row.location_venue = bool(place and place.venue)
            row.airports = format_airports(airports)
            row.airports_location = location
            session.commit()
            if place is None:
                counts["not_found"] += 1
                log(f"{row.id}: {location!r} is not a single place that could be found; no airports")
                continue
            counts["updated"] += 1
            found = ", ".join(f"{a.code} {a.miles} mi" + (" (minor)" if a.minor else "") for a in airports)
            kind = "venue" if place.venue else "city"
            log(f"{row.id}: {location!r} ({kind}) -> {found or 'no airports'}")
    return counts
