"""Nearby airports and the venue flag (``conference_agent.airports``).

Offline: the geocoder, the airport table, and the router are replaced with
fixtures, so no request reaches Nominatim, OurAirports, or OSRM.
"""

import io
import json
from datetime import date

import pytest
from sqlalchemy.orm import Session

from conference_agent import airports as ap
from conference_agent.cli import add_field_schema, main
from conference_agent.database import (
    ConferenceRow,
    get_engine,
    merge_records,
    roll_past_editions,
    upsert_conferences,
)
from conference_agent.models import Conference

# (code, name, lat, lon, large): Chicago airports, one ~90 mi away, one far off.
_TABLE = [
    ("ORD", "Chicago O'Hare International Airport", 41.9786, -87.9048, True),
    ("MDW", "Chicago Midway International Airport", 41.7868, -87.7522, True),
    ("GYY", "Gary/Chicago International Airport", 41.6163, -87.4128, False),
    ("MKE", "Milwaukee Mitchell International Airport", 42.9472, -87.8966, True),
    ("LAX", "Los Angeles International Airport", 33.9425, -118.4081, True),
]
# Annual passengers; MKE has no count (it falls back to OurAirports' class).
_TRAFFIC = {"ORD": 80_000_000, "MDW": 20_000_000, "GYY": 0, "LAX": 75_000_000}
_MCCORMICK = (41.8517, -87.6165)
# Road miles; GYY has no road route.
_ROADS = {"MDW": 10, "ORD": 18, "GYY": None, "MKE": 90, "LAX": 15}


class _FakeRouter:
    def __init__(self, roads=None):
        self.roads = _ROADS if roads is None else roads
        self.asked = []

    def routes(self, lat, lon, airports):
        self.asked.append(sorted(airports))
        return {c: self.roads.get(c) for c in airports}


class _FakeGeocoder:
    """Answers queries from a dict; records what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def search(self, query, settlement=False):
        key = ("settlement:" if settlement else "") + query
        self.asked.append(key)
        return self.answers.get(key)


def _hit(lat, lon, category, size=0.01, name="", addresstype=""):
    return {"lat": lat, "lon": lon, "category": category, "name": name, "addresstype": addresstype,
            "bbox": [lat - size, lat + size, lon - size, lon + size]}


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _row(url, row_id):
    with Session(get_engine(url)) as session:
        row = session.get(ConferenceRow, row_id)
        session.expunge(row)
        return row


# --- the stored form ------------------------------------------------------------


def test_stored_form_round_trips_with_the_minor_marker():
    airports = [ap.Airport("MDW", "Midway", 10), ap.Airport("GYY", "Gary/Chicago", 19, minor=True)]
    text = ap.format_airports(airports)
    assert text == "MDW | Midway | 10 mi\nGYY | Gary/Chicago | 19 mi | minor"
    assert ap.parse_airports(text) == airports
    # Former forms (bare miles, a driving time, "small") still read.
    gyy = [ap.Airport("GYY", "Gary/Chicago", 19, minor=True)]
    assert ap.parse_airports("GYY | Gary/Chicago | 19 | small") == gyy
    assert ap.parse_airports("GYY | Gary/Chicago | 19 mi | 30 min | minor") == gyy
    assert ap.parse_airports("MDW | Midway | 10 mi | 20 min") == [ap.Airport("MDW", "Midway", 10)]
    assert ap.format_airports([]) is None
    assert ap.parse_airports(None) == []


def test_tier_is_by_passengers_else_ourairports_class():
    assert ap.airport_tier(10_000_000, large=False) == 0
    assert ap.airport_tier(9_999_999, large=True) == 1
    assert ap.airport_tier(500_000, large=False) == 1
    assert ap.airport_tier(499_999, large=True) == 2
    assert ap.airport_tier(None, large=True) == 1
    assert ap.airport_tier(None, large=False) == 2


def _minor(entries, passengers=None):
    """``mark_minor`` over ``(code, miles, tier[, routed])`` entries."""
    airports = [ap.Airport(e[0], e[0], e[1]) for e in entries]
    routed = {e[0] for e in entries if len(e) < 4 or e[3]}
    return ap.mark_minor(airports, {e[0]: e[2] for e in entries}, passengers or {}, routed)


def test_main_airports_are_the_nearby_ones_of_the_busiest_tier():
    # Cold Spring Harbor: LGA and JFK are main; Newark, a hub but much farther
    # by road, and the busy-enough regional airports are minor.
    csh = [("ISP", 23, 1), ("HPN", 34, 1), ("JFK", 30, 0), ("LGA", 28, 0),
           ("EWR", 46, 0), ("BDL", 110, 1)]
    traffic = {"JFK": 62_000_000, "LGA": 33_000_000, "EWR": 49_000_000}
    assert _minor(csh, traffic) == {"ISP", "HPN", "EWR", "BDL"}
    # McCormick Place: Midway and O'Hare are both close.
    assert _minor([("MDW", 11, 0), ("ORD", 21, 0), ("MKE", 90, 1)]) == {"MKE"}
    # No hub nearby: the nearest major airport is main.
    assert _minor([("MSN", 6, 1), ("MKE", 75, 1), ("XYZ", 3, 2)]) == {"MKE", "XYZ"}
    # Only tiny airports: the nearest is still shown.
    assert _minor([("AAA", 30, 2), ("BBB", 80, 2)]) == {"BBB"}
    # No road routes at all: straight-line miles decide.
    assert _minor([("AAA", 30, 1, False), ("BBB", 80, 1, False)]) == {"BBB"}
    # An airport with no road route is minor when others have one.
    assert _minor([("AAA", 20, 1), ("BBB", 10, 0, False)]) == {"BBB"}
    assert _minor([]) == set()


def test_a_far_busier_hub_is_main_within_the_cap():
    seoul = [("GMP", 15, 0), ("ICN", 39, 0)]
    assert _minor(seoul, {"GMP": 25_000_000, "ICN": 70_000_000}) == set()
    # Not twice as busy: still minor.
    assert _minor(seoul, {"GMP": 25_000_000, "ICN": 45_000_000}) == {"ICN"}
    # Twice as busy but beyond the cap: still minor.
    far = [("GMP", 15, 0), ("ICN", 80, 0)]
    assert _minor(far, {"GMP": 25_000_000, "ICN": 70_000_000}) == {"ICN"}


def test_router_subtracts_the_airside_tail_and_caches(tmp_path, monkeypatch):
    calls = []
    table = {
        "code": "Ok",
        "durations": [[0, 3600.0, None, 600.0]],
        "distances": [[0, 38_000.0, None, 8000.0]],
        "destinations": [{"distance": 1}, {"distance": 5}, {"distance": 5}, {"distance": 9000}],
    }
    # Expressway at ~42 mph, then 20 minutes of 9-mph airside roads.
    route = {"code": "Ok", "routes": [{"legs": [{"steps": [
        {"duration": 2400.0, "distance": 45_000.0},
        {"duration": 600.0, "distance": 2400.0},
        {"duration": 600.0, "distance": 2400.0},
        {"duration": 0.0, "distance": 0.0},
    ]}]}]}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(request, timeout=None):
        calls.append(request.full_url)
        return Resp(json.dumps(table if "/table/" in request.full_url else route).encode())

    monkeypatch.setattr(ap.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ap, "_OSRM_INTERVAL_S", 0)
    router = ap._Router(tmp_path / "drive.json")
    points = {"AAA": (41.0, -87.0), "BBB": (42.0, -87.0), "CCC": (41.5, -87.5)}
    # AAA: 23.6 road miles less a 3-mile airside tail. BBB has no route; CCC
    # is far from any road.
    assert router.routes(41.8, -87.6, points) == {"AAA": 21, "BBB": None, "CCC": None}
    assert calls[0].startswith(ap.OSRM_TABLE_URL + "-87.600000,41.800000;-87.000000,41.000000")
    assert calls[1].startswith(ap.OSRM_ROUTE_URL) and len(calls) == 2
    # Cached, also across instances.
    assert ap._Router(tmp_path / "drive.json").routes(41.8, -87.6, points)["AAA"] == 21
    assert len(calls) == 2


def test_a_drive_slow_all_the_way_has_no_tail(monkeypatch):
    slow = {"code": "Ok", "routes": [{"legs": [{"steps": [{"duration": 600.0, "distance": 2000.0}]}]}]}
    router = ap._Router.__new__(ap._Router)
    monkeypatch.setattr(router, "_get", lambda url: slow, raising=False)
    assert router._tail(41.8, -87.6, (41.0, -87.0)) == [0, 0]


def test_wikidata_counts_use_the_largest_recent_figure(monkeypatch):
    year = date.today().year

    def row(code, count=None, when=None):
        b = {"iata": {"value": code}}
        if count is not None:
            b["count"] = {"value": str(count)}
        if when is not None:
            b["when"] = {"value": f"{when}-01-01T00:00:00Z"}
        return b

    bindings = [
        row("AAA", 400_000, year - 1), row("AAA", 600_000, year - 2),
        row("BBB", 9_000_000, year - 30), row("BBB", 100_000, year - 1),  # old peak ignored
        row("CCC"),  # no count
    ]
    body = json.dumps({"results": {"bindings": bindings}}).encode()
    monkeypatch.setattr(ap.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(body))
    assert ap._fetch_passengers(["AAA", "BBB", "CCC", "DDD"]) == {
        "AAA": 600_000, "BBB": 100_000, "CCC": None, "DDD": None,
    }


# --- geocoding -----------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["", "TBA", "Virtual", "Milan, Italy (upcoming 2027); Stockholm, Sweden (prior 2026)",
     "Sydney, Australia / Paris, France / Atlanta, GA (three sites)"],
)
def test_single_place_rejects_placeholders_and_several_places(location):
    assert ap.single_place(location) is None


def test_single_place_keeps_one_city_with_a_slash_in_the_venue():
    loc = "Van Andel Institute / Grand Valley State University, Grand Rapids, MI"
    assert ap.single_place(loc) == loc


def test_venue_is_a_specific_place_on_the_full_location():
    geo = _FakeGeocoder({"McCormick Place, Chicago, IL": _hit(*_MCCORMICK, "amenity")})
    place = ap.geocode("McCormick Place, Chicago, IL", geo)
    assert place == ap.Place(*_MCCORMICK, venue=True)


def test_city_is_not_a_venue():
    geo = _FakeGeocoder({"Chicago, IL": _hit(41.88, -87.62, "boundary", size=0.2)})
    assert ap.geocode("Chicago, IL", geo).venue is False


def test_fallback_to_the_city_is_not_a_venue():
    # The venue is not found; dropping it finds the city, a place, not a venue.
    geo = _FakeGeocoder({"Chicago, IL": _hit(41.88, -87.62, "amenity")})
    place = ap.geocode("Some Unknown Hall, Chicago, IL", geo)
    assert place.venue is False
    assert geo.asked == ["Some Unknown Hall, Chicago, IL", "Chicago, IL"]


def test_a_settlement_named_like_a_station_is_not_a_venue():
    # "Hengqin, China" matches Hengqin railway station, but Hengqin is a town.
    geo = _FakeGeocoder({
        "Hengqin, China": _hit(22.14, 113.54, "railway", name="Hengqin"),
        "settlement:Hengqin, China": _hit(22.13, 113.52, "place", name="Hengqin", addresstype="town"),
    })
    assert ap.geocode("Hengqin, China", geo).venue is False


def test_a_venue_finds_a_settlement_of_another_name():
    geo = _FakeGeocoder({
        "McCormick Place, Chicago, IL": _hit(*_MCCORMICK, "amenity", name="McCormick Place"),
        "settlement:McCormick Place, Chicago, IL": _hit(41.88, -87.62, "boundary", name="Chicago"),
    })
    assert ap.geocode("McCormick Place, Chicago, IL", geo).venue is True


def test_a_wide_city_is_still_used():
    # Tokyo's boundary takes in far-off islands; a city is used regardless.
    geo = _FakeGeocoder({"Tokyo, Japan": _hit(35.68, 139.76, "boundary", size=8, addresstype="city")})
    assert ap.geocode("Tokyo, Japan", geo) is not None


def test_parenthetical_venue_is_tried_first():
    geo = _FakeGeocoder({"San Diego Convention Center, San Diego, CA": _hit(32.7, -117.16, "building")})
    assert ap.geocode("San Diego, CA (San Diego Convention Center)", geo).venue is True


def test_a_state_sized_match_is_not_used():
    # "NY" alone geocodes to the whole state: too wide for "within 100 miles".
    geo = _FakeGeocoder({"NY": _hit(43.0, -75.0, "boundary", size=3)})
    assert ap.geocode("Unknown Lab, NY", geo) is None


# --- airports ------------------------------------------------------------------


def test_nearby_airports_within_radius_nearest_by_road_first():
    found = ap.nearby_airports(*_MCCORMICK, table=_TABLE, traffic=_TRAFFIC, router=_FakeRouter())
    # LAX is beyond the radius; GYY, with no road route, keeps its straight-line miles.
    assert [(a.code, a.miles) for a in found] == [("MDW", 10), ("ORD", 18), ("GYY", 19), ("MKE", 90)]
    assert [a.minor for a in found] == [False, False, True, True]


def test_airports_beyond_100_road_miles_are_dropped_unless_none_is_closer():
    roads = {**_ROADS, "MKE": 130}
    found = ap.nearby_airports(*_MCCORMICK, table=_TABLE, traffic=_TRAFFIC, router=_FakeRouter(roads))
    assert [a.code for a in found] == ["MDW", "ORD", "GYY"]
    # Every candidate is over 100 road miles: the nearest is kept, as main.
    far = {"ORD": 140, "MDW": 120, "GYY": 150, "MKE": 200}
    found = ap.nearby_airports(*_MCCORMICK, table=_TABLE, traffic=_TRAFFIC, router=_FakeRouter(far))
    assert [(a.code, a.miles, a.minor) for a in found] == [("MDW", 120, False)]


def test_with_no_airport_within_the_radius_the_nearest_beyond_is_listed():
    router = _FakeRouter({"ORD": 260, "MDW": 250})
    # A point about 170 miles south of Chicago.
    found = ap.nearby_airports(39.4, -87.6, table=_TABLE[:2], traffic=_TRAFFIC, router=router)
    assert [(a.code, a.miles, a.minor) for a in found] == [("MDW", 250, False)]
    assert router.asked == [["MDW", "ORD"]]


def test_distance_is_great_circle_miles():
    # Chicago to Los Angeles is about 1,745 miles.
    assert 1700 < ap.distance_miles(41.88, -87.62, 34.05, -118.24) < 1790


# --- the database: staleness, entry, and the lookup ----------------------------


def _conf(**kw):
    base = {"acronym": "RSNA", "name": "Radiological Society Meeting", "subcategory": "radiology"}
    return Conference(**{**base, **kw})


@pytest.fixture
def offline(monkeypatch):
    """Route lookups to the fixtures."""
    answers = {
        "McCormick Place, Chicago, IL": _hit(*_MCCORMICK, "amenity"),
        "Los Angeles, CA": _hit(34.05, -118.24, "boundary", size=0.3),
    }
    monkeypatch.setattr(ap, "_Geocoder", lambda: _FakeGeocoder(answers))
    monkeypatch.setattr(ap, "_airport_table", lambda path=None: _TABLE)

    class Traffic:
        def passengers(self, codes):
            return {c: _TRAFFIC.get(c) for c in codes}

    monkeypatch.setattr(ap, "_Traffic", Traffic)
    monkeypatch.setattr(ap, "_Router", _FakeRouter)


def test_refresh_fills_airports_and_venue_then_skips_unchanged(tmp_path, offline):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(upcoming_location="McCormick Place, Chicago, IL")], db_url=url)
    counts = ap.refresh_airports(db_url=url, log=lambda _: None)
    assert counts == {"updated": 1, "not_found": 0, "failed": 0}
    row = _row(url, "radiological-society-meeting")
    assert row.airports.splitlines()[0] == "MDW | Chicago Midway International Airport | 10 mi"
    assert row.airports.splitlines()[2] == "GYY | Gary/Chicago International Airport | 19 mi | minor"
    assert row.airports_location == "McCormick Place, Chicago, IL"
    assert row.location_venue is True
    # Nothing changed: nothing to look up.
    assert ap.refresh_airports(db_url=url, log=lambda _: None)["updated"] == 0


def test_a_changed_location_clears_airports_until_looked_up_again(tmp_path, offline):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(upcoming_location="McCormick Place, Chicago, IL")], db_url=url)
    ap.refresh_airports(db_url=url, log=lambda _: None)

    # A discovery write that moves the meeting clears the stale list...
    upsert_conferences([_conf(upcoming_location="Los Angeles, CA")], db_url=url)
    row = _row(url, "radiological-society-meeting")
    assert (row.airports, row.airports_location, row.location_venue) == (None, None, None)
    # ...and the next lookup finds the new one.
    ap.refresh_airports(db_url=url, log=lambda _: None)
    row = _row(url, "radiological-society-meeting")
    assert row.airports.startswith("LAX |")
    assert row.location_venue is False


def test_merge_with_a_new_location_clears_airports(tmp_path, offline):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(upcoming_location="McCormick Place, Chicago, IL")], db_url=url)
    ap.refresh_airports(db_url=url, log=lambda _: None)
    merge_records([{"id": "radiological-society-meeting", "upcoming_location": "Los Angeles, CA"}], db_url=url)
    assert _row(url, "radiological-society-meeting").airports is None


def test_rolling_an_edition_keeps_airports_when_the_shown_location_is_the_same(tmp_path, offline):
    url = _db_url(tmp_path)
    upsert_conferences(
        [_conf(upcoming_location="McCormick Place, Chicago, IL",
               upcoming_start_date="2020-11-29", upcoming_end_date="2020-12-03")],
        db_url=url,
    )
    ap.refresh_airports(db_url=url, log=lambda _: None)
    assert roll_past_editions(url)["rolled"] == ["radiological-society-meeting"]
    row = _row(url, "radiological-society-meeting")
    assert row.prior_location == "McCormick Place, Chicago, IL"
    assert row.airports is not None


def test_unfound_location_is_recorded_without_airports(tmp_path, offline):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(upcoming_location="TBA")], db_url=url)
    assert ap.refresh_airports(db_url=url, log=lambda _: None)["not_found"] == 1
    row = _row(url, "radiological-society-meeting")
    assert row.airports is None and row.airports_location == "TBA" and row.location_venue is False


def test_unreachable_service_leaves_the_row_for_the_next_run(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    upsert_conferences([_conf(upcoming_location="Chicago, IL")], db_url=url)

    class Down:
        def search(self, query):
            raise OSError("network down")

    monkeypatch.setattr(ap, "_Geocoder", Down)
    assert ap.refresh_airports(db_url=url, log=lambda _: None)["failed"] == 1
    assert _row(url, "radiological-society-meeting").airports_location is None


# --- `add`: airports are derived, never entered ------------------------------


def test_add_has_no_airports_input(tmp_path, capsys):
    url = _db_url(tmp_path)
    with pytest.raises(SystemExit):
        main(["--db", url, "add", "--conference-name", "X", "--airports", "ORD", "O'Hare", "17"])
    assert all(f["name"] != "airports" for f in add_field_schema()["fields"])
    assert any(d["name"] == "airports" for d in add_field_schema()["derived"])


def test_a_csv_export_with_airports_reimports_without_them(tmp_path):
    url = _db_url(tmp_path)
    path = tmp_path / "in.csv"
    path.write_text(
        "conference_name,subcategory,location,airports\n"
        'Radiological Society Meeting,radiology,"Chicago, IL","ORD | O\'Hare | 17"\n'
    )
    assert main(["--db", url, "add", "--csv", str(path)]) == 0
    row = _row(url, "radiological-society-meeting")
    assert row.upcoming_location == "Chicago, IL" and row.airports is None
