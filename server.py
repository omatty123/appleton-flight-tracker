#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import mimetypes
import os
import posixpath
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
DATA_DIR = BASE_DIR / "data"
OPEN_SKY_STATES_URL = "https://opensky-network.org/api/states/all"
OPEN_SKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
ADSBDB_BASE_URL = "https://api.adsbdb.com/v0"
USER_AGENT = "appleton-flight-tracker/1.0"
METERS_TO_FEET = 3.280839895
MS_TO_MPH = 2.2369362921
MS_TO_FPM = 196.8503937
DEFAULT_NTFY_SERVER = "https://ntfy.sh"


def load_env() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


@dataclass(frozen=True)
class Settings:
    label: str
    home_lat: float
    home_lon: float
    search_radius_miles: float
    overhead_radius_miles: float
    poll_interval_seconds: int
    port: int
    db_path: Path
    opensky_client_id: str
    opensky_client_secret: str
    opensky_timeout_seconds: int
    adsbdb_enabled: bool
    adsbdb_cache_hours: int
    adsbdb_timeout_seconds: int
    adsbdb_max_enrich_per_refresh: int
    collect_min_speed_mph: float
    collect_min_altitude_ft: float
    notify_enabled: bool
    ntfy_server: str
    ntfy_topic: str
    notify_min_speed_mph: float
    notify_min_altitude_ft: float
    notify_radius_miles: float
    notify_approach_degrees: float
    notify_cooldown_minutes: int
    notify_anomalies_enabled: bool
    anomaly_low_altitude_ft: float
    anomaly_min_speed_mph: float
    anomaly_radius_miles: float


def load_settings() -> Settings:
    load_env()
    db_value = os.environ.get("FLIGHT_TRACKER_DB", "data/flights.sqlite3")
    db_path = Path(db_value)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return Settings(
        label=os.environ.get("LOCATION_LABEL", "Appleton, WI"),
        home_lat=env_float("HOME_LAT", 44.2619),
        home_lon=env_float("HOME_LON", -88.4154),
        search_radius_miles=env_float("SEARCH_RADIUS_MILES", 45.0),
        overhead_radius_miles=env_float("OVERHEAD_RADIUS_MILES", 8.0),
        poll_interval_seconds=max(30, env_int("POLL_INTERVAL_SECONDS", 120)),
        port=env_int("PORT", 8787),
        db_path=db_path,
        opensky_client_id=os.environ.get("OPENSKY_CLIENT_ID", "").strip(),
        opensky_client_secret=os.environ.get("OPENSKY_CLIENT_SECRET", "").strip(),
        opensky_timeout_seconds=max(3, env_int("OPENSKY_TIMEOUT_SECONDS", 12)),
        adsbdb_enabled=os.environ.get("ADSBDB_ENABLED", "1").strip().lower()
        not in {"0", "false", "no"},
        adsbdb_cache_hours=max(1, env_int("ADSBDB_CACHE_HOURS", 12)),
        adsbdb_timeout_seconds=max(2, env_int("ADSBDB_TIMEOUT_SECONDS", 8)),
        adsbdb_max_enrich_per_refresh=max(
            0, env_int("ADSBDB_MAX_ENRICH_PER_REFRESH", 12)
        ),
        collect_min_speed_mph=env_float("COLLECT_MIN_SPEED_MPH", 300.0),
        collect_min_altitude_ft=env_float("COLLECT_MIN_ALTITUDE_FT", 10000.0),
        notify_enabled=os.environ.get("NOTIFY_ENABLED", "0").strip().lower()
        in {"1", "true", "yes"},
        ntfy_server=os.environ.get("NTFY_SERVER", DEFAULT_NTFY_SERVER)
        .strip()
        .rstrip("/"),
        ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip(),
        notify_min_speed_mph=env_float("NOTIFY_MIN_SPEED_MPH", 300.0),
        notify_min_altitude_ft=env_float("NOTIFY_MIN_ALTITUDE_FT", 10000.0),
        notify_radius_miles=env_float("NOTIFY_RADIUS_MILES", 25.0),
        notify_approach_degrees=env_float("NOTIFY_APPROACH_DEGREES", 45.0),
        notify_cooldown_minutes=max(5, env_int("NOTIFY_COOLDOWN_MINUTES", 30)),
        notify_anomalies_enabled=os.environ.get("NOTIFY_ANOMALIES", "1")
        .strip()
        .lower()
        not in {"0", "false", "no"},
        anomaly_low_altitude_ft=env_float("ANOMALY_LOW_ALTITUDE_FT", 2500.0),
        anomaly_min_speed_mph=env_float("ANOMALY_MIN_SPEED_MPH", 180.0),
        anomaly_radius_miles=env_float("ANOMALY_RADIUS_MILES", 10.0),
    )


settings = load_settings()
DATA_DIR.mkdir(exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)

poll_lock = threading.Lock()
token_lock = threading.Lock()
last_poll_at = 0.0
last_status: dict[str, Any] = {
    "ok": None,
    "message": "No poll has completed yet.",
    "aircraft_count": 0,
    "source": "startup",
    "at": None,
}
token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
shutdown_event = threading.Event()


CATEGORY_LABELS = {
    0: "No category",
    1: "Light aircraft",
    2: "Small aircraft",
    3: "Large aircraft",
    4: "High vortex large aircraft",
    5: "Heavy aircraft",
    6: "High performance aircraft",
    7: "Rotorcraft",
    8: "Glider",
    9: "Lighter-than-air",
    10: "Parachutist",
    11: "Ultralight",
    12: "Reserved",
    13: "UAV",
    14: "Space/transatmospheric",
    15: "Surface emergency vehicle",
    16: "Surface service vehicle",
    17: "Point obstacle",
    18: "Cluster obstacle",
    19: "Line obstacle",
}


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at INTEGER NOT NULL,
                response_time INTEGER,
                icao24 TEXT NOT NULL,
                callsign TEXT,
                origin_country TEXT,
                longitude REAL,
                latitude REAL,
                baro_altitude_m REAL,
                geo_altitude_m REAL,
                on_ground INTEGER,
                velocity_ms REAL,
                true_track_deg REAL,
                vertical_rate_ms REAL,
                squawk TEXT,
                position_source INTEGER,
                category INTEGER,
                distance_miles REAL,
                bearing_deg REAL,
                overhead INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                UNIQUE (observed_at, icao24)
            );

            CREATE INDEX IF NOT EXISTS idx_observations_time
                ON observations (observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_observations_aircraft_time
                ON observations (icao24, observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_observations_overhead_time
                ON observations (overhead, observed_at DESC);

            CREATE TABLE IF NOT EXISTS api_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                ok INTEGER NOT NULL,
                status_code INTEGER,
                message TEXT NOT NULL,
                aircraft_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_api_events_time
                ON api_events (created_at DESC);

            CREATE TABLE IF NOT EXISTS aircraft_enrichment (
                cache_key TEXT PRIMARY KEY,
                icao24 TEXT NOT NULL,
                callsign TEXT,
                fetched_at INTEGER NOT NULL,
                ok INTEGER NOT NULL,
                status_code INTEGER,
                message TEXT NOT NULL,
                raw_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_aircraft_enrichment_fetch
                ON aircraft_enrichment (fetched_at DESC);

            CREATE TABLE IF NOT EXISTS notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                observed_at INTEGER,
                icao24 TEXT,
                callsign TEXT,
                rule TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                priority TEXT NOT NULL,
                sent_ok INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                payload_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_notification_events_time
                ON notification_events (created_at DESC);
            """
        )


def miles_bbox(lat: float, lon: float, radius_miles: float) -> dict[str, float]:
    lat_delta = radius_miles / 69.0
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    lon_delta = radius_miles / (69.172 * cos_lat)
    return {
        "lamin": lat - lat_delta,
        "lamax": lat + lat_delta,
        "lomin": lon - lon_delta,
        "lomax": lon + lon_delta,
    }


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_miles = 3958.7613
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius_miles * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        d_lambda
    )
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angular_difference_deg(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    return abs((first - second + 180) % 360 - 180)


def cardinal(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    directions = [
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    ]
    return directions[int((degrees + 11.25) / 22.5) % 16]


def meters_to_feet(value: float | None) -> float | None:
    return None if value is None else value * METERS_TO_FEET


def ms_to_mph(value: float | None) -> float | None:
    return None if value is None else value * MS_TO_MPH


def ms_to_fpm(value: float | None) -> float | None:
    return None if value is None else value * MS_TO_FPM


def collection_speed_threshold_ms() -> float:
    return settings.collect_min_speed_mph / MS_TO_MPH


def collection_altitude_threshold_m() -> float:
    return settings.collect_min_altitude_ft / METERS_TO_FEET


def clean_callsign(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def airport_label(airport: dict[str, Any] | None) -> str | None:
    if not airport:
        return None
    code = airport.get("iata_code") or airport.get("icao_code")
    municipality = airport.get("municipality")
    name = airport.get("name")
    if code and municipality:
        return f"{municipality} ({code})"
    if code and name:
        return f"{name} ({code})"
    return name or municipality or code


def route_airport_payload(airport: dict[str, Any] | None) -> dict[str, Any] | None:
    if not airport:
        return None
    return {
        "name": airport.get("name"),
        "municipality": airport.get("municipality"),
        "iata": airport.get("iata_code"),
        "icao": airport.get("icao_code"),
        "lat": airport.get("latitude"),
        "lon": airport.get("longitude"),
        "label": airport_label(airport),
    }


def route_from_enrichment(enrichment: dict[str, Any] | None) -> dict[str, Any] | None:
    if not enrichment or not enrichment.get("ok"):
        return None
    response = enrichment.get("response") or {}
    route = response.get("flightroute")
    if not route:
        return None

    return {
        "callsign": route.get("callsign"),
        "callsign_iata": route.get("callsign_iata"),
        "callsign_icao": route.get("callsign_icao"),
        "airline": route.get("airline"),
        "origin": route_airport_payload(route.get("origin")),
        "midpoint": route_airport_payload(route.get("midpoint")),
        "destination": route_airport_payload(route.get("destination")),
    }


def aircraft_from_enrichment(enrichment: dict[str, Any] | None) -> dict[str, Any] | None:
    if not enrichment or not enrichment.get("ok"):
        return None
    aircraft = (enrichment.get("response") or {}).get("aircraft")
    if not aircraft:
        return None
    return {
        "type": aircraft.get("type"),
        "icao_type": aircraft.get("icao_type"),
        "manufacturer": aircraft.get("manufacturer"),
        "registration": aircraft.get("registration"),
        "owner": aircraft.get("registered_owner"),
        "owner_country": aircraft.get("registered_owner_country_name"),
        "photo": aircraft.get("url_photo"),
        "photo_thumbnail": aircraft.get("url_photo_thumbnail"),
    }


def enrichment_cache_key(icao24: str, callsign: str | None) -> str:
    callsign_part = (callsign or "").strip().upper()
    return f"{icao24.lower()}|{callsign_part}"


def parse_enrichment_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload: dict[str, Any] = {}
    if row["raw_json"]:
        try:
            payload = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            payload = {}
    response = payload.get("response") if isinstance(payload, dict) else None
    return {
        "ok": bool(row["ok"]),
        "status_code": row["status_code"],
        "message": row["message"],
        "fetched_at": row["fetched_at"],
        "response": response or {},
    }


def fetch_adsbdb_aircraft(
    icao24: str,
    callsign: str | None,
) -> tuple[int | None, dict[str, Any]]:
    path = f"/aircraft/{urllib.parse.quote(icao24.upper())}"
    query = ""
    if callsign:
        query = "?" + urllib.parse.urlencode({"callsign": callsign.strip().upper()})
    url = f"{ADSBDB_BASE_URL}{path}{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(
        request, timeout=settings.adsbdb_timeout_seconds
    ) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def fetch_adsbdb_callsign(callsign: str) -> tuple[int | None, dict[str, Any]]:
    path = f"/callsign/{urllib.parse.quote(callsign.strip().upper())}"
    url = f"{ADSBDB_BASE_URL}{path}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(
        request, timeout=settings.adsbdb_timeout_seconds
    ) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def merge_adsbdb_payloads(
    aircraft_payload: dict[str, Any] | None,
    route_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(aircraft_payload or {})
    merged_response = dict((aircraft_payload or {}).get("response") or {})
    route_response = (route_payload or {}).get("response") or {}
    if route_response.get("flightroute"):
        merged_response["flightroute"] = route_response["flightroute"]
    if route_response.get("aircraft") and not merged_response.get("aircraft"):
        merged_response["aircraft"] = route_response["aircraft"]
    merged["response"] = merged_response
    return merged


def fetch_adsbdb(icao24: str, callsign: str | None) -> tuple[int | None, dict[str, Any]]:
    status_code, payload = fetch_adsbdb_aircraft(icao24, callsign)
    if callsign and not (payload.get("response") or {}).get("flightroute"):
        route_status, route_payload = fetch_adsbdb_callsign(callsign)
        return route_status or status_code, merge_adsbdb_payloads(payload, route_payload)
    return status_code, payload


def store_enrichment(
    icao24: str,
    callsign: str | None,
    ok: bool,
    message: str,
    status_code: int | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    cache_key = enrichment_cache_key(icao24, callsign)
    fetched_at = int(time.time())
    raw_json = json.dumps(payload, separators=(",", ":")) if payload else None
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO aircraft_enrichment
                (cache_key, icao24, callsign, fetched_at, ok, status_code, message, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                icao24 = excluded.icao24,
                callsign = excluded.callsign,
                fetched_at = excluded.fetched_at,
                ok = excluded.ok,
                status_code = excluded.status_code,
                message = excluded.message,
                raw_json = excluded.raw_json
            """,
            (
                cache_key,
                icao24.lower(),
                callsign.strip().upper() if callsign else None,
                fetched_at,
                1 if ok else 0,
                status_code,
                message,
                raw_json,
            ),
        )

    return {
        "ok": ok,
        "status_code": status_code,
        "message": message,
        "fetched_at": fetched_at,
        "response": (payload or {}).get("response") or {},
    }


def get_enrichment(
    icao24: str,
    callsign: str | None,
    allow_fetch: bool = True,
) -> dict[str, Any] | None:
    if not settings.adsbdb_enabled or not icao24:
        return None

    cache_key = enrichment_cache_key(icao24, callsign)
    now = int(time.time())
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM aircraft_enrichment
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()

    if row:
        max_age = settings.adsbdb_cache_hours * 3600 if row["ok"] else 120
        if now - row["fetched_at"] < max_age or not allow_fetch:
            return parse_enrichment_row(row)

    if not allow_fetch:
        return parse_enrichment_row(row)

    status_code = None
    try:
        status_code, payload = fetch_adsbdb(icao24, callsign)
        return store_enrichment(
            icao24,
            callsign,
            True,
            "ADSBDB enrichment found.",
            status_code,
            payload,
        )
    except urllib.error.HTTPError as exc:
        payload = None
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - optional error body only.
            payload = None
        if callsign:
            try:
                route_status, route_payload = fetch_adsbdb_callsign(callsign)
                return store_enrichment(
                    icao24,
                    callsign,
                    True,
                    "ADSBDB route found by callsign.",
                    route_status,
                    route_payload,
                )
            except Exception:  # noqa: BLE001 - store original aircraft lookup failure.
                pass
        return store_enrichment(
            icao24,
            callsign,
            False,
            f"ADSBDB returned HTTP {exc.code}.",
            exc.code,
            payload,
        )
    except Exception as exc:  # noqa: BLE001 - enrichment should never break tracking.
        return store_enrichment(
            icao24,
            callsign,
            False,
            f"ADSBDB enrichment failed: {exc}",
            status_code,
            None,
        )


def state_to_observation(state: list[Any], observed_at: int) -> dict[str, Any] | None:
    if len(state) < 17:
        return None

    icao24 = state[0]
    longitude = state[5]
    latitude = state[6]
    if not icao24 or latitude is None or longitude is None:
        return None

    distance = haversine_miles(settings.home_lat, settings.home_lon, latitude, longitude)
    if distance > settings.search_radius_miles:
        return None

    on_ground = 1 if state[8] else 0
    velocity_ms = state[9]
    altitude_m = state[13] if state[13] is not None else state[7]
    if (
        on_ground
        or velocity_ms is None
        or altitude_m is None
        or velocity_ms < collection_speed_threshold_ms()
        or altitude_m < collection_altitude_threshold_m()
    ):
        return None

    bearing = bearing_deg(settings.home_lat, settings.home_lon, latitude, longitude)
    category = state[17] if len(state) > 17 else None

    return {
        "observed_at": observed_at,
        "response_time": state[4],
        "icao24": icao24,
        "callsign": clean_callsign(state[1]),
        "origin_country": state[2],
        "longitude": longitude,
        "latitude": latitude,
        "baro_altitude_m": state[7],
        "geo_altitude_m": state[13],
        "on_ground": on_ground,
        "velocity_ms": velocity_ms,
        "true_track_deg": state[10],
        "vertical_rate_ms": state[11],
        "squawk": state[14],
        "position_source": state[16],
        "category": category,
        "distance_miles": distance,
        "bearing_deg": bearing,
        "overhead": 1 if distance <= settings.overhead_radius_miles else 0,
        "raw_json": json.dumps(state, separators=(",", ":")),
    }


def public_aircraft(
    row: sqlite3.Row,
    enrichment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    altitude_m = (
        row["geo_altitude_m"]
        if row["geo_altitude_m"] is not None
        else row["baro_altitude_m"]
    )
    track = row["true_track_deg"]
    bearing = row["bearing_deg"]
    route = route_from_enrichment(enrichment)
    aircraft = aircraft_from_enrichment(enrichment)
    origin = route.get("origin") if route else None
    destination = route.get("destination") if route else None
    airline = route.get("airline") if route else None
    if destination:
        destination_note = "Route enriched from ADSBDB by callsign."
    elif row["callsign"]:
        destination_note = "No route match found in ADSBDB for this callsign yet."
    else:
        destination_note = "Destination requires a callsign; OpenSky live state vectors do not include it."

    return {
        "icao24": row["icao24"],
        "callsign": row["callsign"],
        "origin_country": row["origin_country"],
        "lat": row["latitude"],
        "lon": row["longitude"],
        "altitude_ft": meters_to_feet(altitude_m),
        "baro_altitude_ft": meters_to_feet(row["baro_altitude_m"]),
        "geo_altitude_ft": meters_to_feet(row["geo_altitude_m"]),
        "speed_mph": ms_to_mph(row["velocity_ms"]),
        "velocity_ms": row["velocity_ms"],
        "track_deg": track,
        "track_cardinal": cardinal(track),
        "vertical_rate_fpm": ms_to_fpm(row["vertical_rate_ms"]),
        "distance_miles": row["distance_miles"],
        "bearing_deg": bearing,
        "bearing_cardinal": cardinal(bearing),
        "overhead": bool(row["overhead"]),
        "on_ground": bool(row["on_ground"]),
        "squawk": row["squawk"],
        "position_source": row["position_source"],
        "category": row["category"],
        "category_label": CATEGORY_LABELS.get(row["category"], "Unknown"),
        "last_contact": row["response_time"],
        "observed_at": row["observed_at"],
        "origin": origin["label"] if origin else None,
        "destination": destination["label"] if destination else None,
        "route": route,
        "airline": airline,
        "aircraft": aircraft,
        "destination_note": destination_note,
    }


def observation_aircraft_label(observation: dict[str, Any]) -> str:
    return observation.get("callsign") or observation["icao24"].upper()


def observation_altitude_ft(observation: dict[str, Any]) -> float | None:
    altitude_m = (
        observation["geo_altitude_m"]
        if observation["geo_altitude_m"] is not None
        else observation["baro_altitude_m"]
    )
    return meters_to_feet(altitude_m)


def observation_speed_mph(observation: dict[str, Any]) -> float | None:
    return ms_to_mph(observation["velocity_ms"])


def observation_approach_info(observation: dict[str, Any]) -> dict[str, Any]:
    track = observation["true_track_deg"]
    bearing_to_home = bearing_deg(
        observation["latitude"],
        observation["longitude"],
        settings.home_lat,
        settings.home_lon,
    )
    difference = angular_difference_deg(track, bearing_to_home)
    return {
        "bearing_to_home": bearing_to_home,
        "bearing_to_home_cardinal": cardinal(bearing_to_home),
        "track_difference_deg": difference,
        "approaching": difference is not None
        and difference <= settings.notify_approach_degrees,
    }


def route_summary_for_notification(
    icao24: str,
    callsign: str | None,
) -> str | None:
    enrichment = get_enrichment(icao24, callsign, allow_fetch=True)
    route = route_from_enrichment(enrichment)
    if not route:
        return None
    origin = route.get("origin") or {}
    destination = route.get("destination") or {}
    if origin.get("label") and destination.get("label"):
        return f"{origin['label']} to {destination['label']}"
    if destination.get("label"):
        return f"to {destination['label']}"
    return None


def notification_message(
    observation: dict[str, Any],
    reason: str,
    include_route: bool = True,
) -> str:
    label = observation_aircraft_label(observation)
    speed = observation_speed_mph(observation)
    altitude = observation_altitude_ft(observation)
    approach = observation_approach_info(observation)
    distance = observation["distance_miles"]
    bearing = cardinal(observation["bearing_deg"]) or "nearby"
    route = (
        route_summary_for_notification(observation["icao24"], observation["callsign"])
        if include_route
        else None
    )
    eta_minutes = None
    if speed and speed > 0:
        eta_minutes = max(0.0, distance / speed * 60)

    parts = [
        f"{label}: {reason}",
        f"{fmt_notification_number(distance, 1)} mi {bearing}",
        f"{fmt_notification_number(altitude, 0)} ft",
        f"{fmt_notification_number(speed, 0)} mph",
    ]
    if approach["approaching"] and eta_minutes is not None:
        parts.append(f"roughly {fmt_notification_number(eta_minutes, 0)} min out")
    if route:
        parts.append(route)
    return " | ".join(part for part in parts if part and "--" not in part)


def fmt_notification_number(value: float | None, digits: int) -> str:
    if value is None:
        return "--"
    return f"{value:,.{digits}f}"


def notification_candidates(
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    for observation in observations:
        speed = observation_speed_mph(observation)
        altitude = observation_altitude_ft(observation)
        distance = observation["distance_miles"]
        approach = observation_approach_info(observation)
        label = observation_aircraft_label(observation)

        if (
            speed is not None
            and altitude is not None
            and speed >= settings.notify_min_speed_mph
            and altitude >= settings.notify_min_altitude_ft
            and distance <= settings.notify_radius_miles
            and (observation["overhead"] or approach["approaching"])
        ):
            candidates.append(
                {
                    "rule": "high_fast_approach",
                    "title": f"Look up: {label}",
                    "message": notification_message(
                        observation,
                        "high fast aircraft approaching",
                    ),
                    "priority": "default",
                    "tags": "airplane",
                    "observation": observation,
                    "details": {
                        "speed_mph": speed,
                        "altitude_ft": altitude,
                        **approach,
                    },
                }
            )

        if not settings.notify_anomalies_enabled:
            continue

        squawk = observation.get("squawk")
        if squawk in {"7500", "7600", "7700"}:
            candidates.append(
                {
                    "rule": f"emergency_squawk_{squawk}",
                    "title": f"Aviation alert: {label}",
                    "message": notification_message(
                        observation,
                        f"emergency squawk {squawk}",
                    ),
                    "priority": "high",
                    "tags": "warning,airplane",
                    "observation": observation,
                    "details": {"squawk": squawk, **approach},
                }
            )

        if (
            speed is not None
            and altitude is not None
            and distance <= settings.anomaly_radius_miles
            and speed >= settings.anomaly_min_speed_mph
            and altitude <= settings.anomaly_low_altitude_ft
            and not observation["on_ground"]
        ):
            candidates.append(
                {
                    "rule": "low_fast_close",
                    "title": f"Unusual close pass: {label}",
                    "message": notification_message(
                        observation,
                        "low fast close pass",
                    ),
                    "priority": "high",
                    "tags": "warning,airplane",
                    "observation": observation,
                    "details": {
                        "speed_mph": speed,
                        "altitude_ft": altitude,
                        **approach,
                    },
                }
            )

    return candidates


def notification_event_key(candidate: dict[str, Any]) -> str:
    observation = candidate["observation"]
    cooldown_seconds = settings.notify_cooldown_minutes * 60
    bucket = int(observation["observed_at"] // cooldown_seconds)
    return f"{candidate['rule']}:{observation['icao24']}:{bucket}"


def reserve_notification(candidate: dict[str, Any]) -> int | None:
    observation = candidate["observation"]
    event_key = notification_event_key(candidate)
    payload = {
        "rule": candidate["rule"],
        "tags": candidate["tags"],
        "observation": observation,
        "details": candidate["details"],
    }
    with connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO notification_events
                (
                    event_key,
                    created_at,
                    observed_at,
                    icao24,
                    callsign,
                    rule,
                    title,
                    message,
                    priority,
                    payload_json
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                int(time.time()),
                observation["observed_at"],
                observation["icao24"],
                observation["callsign"],
                candidate["rule"],
                candidate["title"],
                candidate["message"],
                candidate["priority"],
                json.dumps(payload, separators=(",", ":")),
            ),
        )
        if cursor.rowcount == 0:
            return None
        return cursor.lastrowid


def update_notification_result(
    notification_id: int,
    sent_ok: bool,
    error: str | None,
) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE notification_events
            SET sent_ok = ?, error = ?
            WHERE id = ?
            """,
            (1 if sent_ok else 0, error, notification_id),
        )


def send_ntfy_notification(candidate: dict[str, Any]) -> tuple[bool, str | None]:
    if not settings.ntfy_topic:
        return False, "NTFY_TOPIC is not configured."
    if not settings.ntfy_server:
        return False, "NTFY_SERVER is not configured."

    topic = urllib.parse.quote(settings.ntfy_topic, safe="")
    url = f"{settings.ntfy_server}/{topic}"
    headers = {
        "User-Agent": USER_AGENT,
        "Title": candidate["title"],
        "Priority": candidate["priority"],
        "Tags": candidate["tags"],
    }
    request = urllib.request.Request(
        url,
        data=candidate["message"].encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if 200 <= response.status < 300:
                return True, None
            return False, f"ntfy returned HTTP {response.status}."
    except urllib.error.HTTPError as exc:
        return False, f"ntfy returned HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"Could not reach ntfy: {exc.reason}"


def process_notifications(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not settings.notify_enabled or not settings.ntfy_topic:
        return []

    results: list[dict[str, Any]] = []
    for candidate in notification_candidates(observations):
        notification_id = reserve_notification(candidate)
        if notification_id is None:
            continue
        sent_ok, error = send_ntfy_notification(candidate)
        update_notification_result(notification_id, sent_ok, error)
        results.append(
            {
                "id": notification_id,
                "rule": candidate["rule"],
                "title": candidate["title"],
                "sent_ok": sent_ok,
                "error": error,
            }
        )
    return results


def notification_config() -> dict[str, Any]:
    return {
        "enabled": settings.notify_enabled,
        "configured": bool(settings.ntfy_topic),
        "provider": "ntfy",
        "ntfy_server": settings.ntfy_server,
        "topic_set": bool(settings.ntfy_topic),
        "rules": {
            "high_fast_approach": {
                "min_speed_mph": settings.notify_min_speed_mph,
                "min_altitude_ft": settings.notify_min_altitude_ft,
                "radius_miles": settings.notify_radius_miles,
                "approach_degrees": settings.notify_approach_degrees,
                "cooldown_minutes": settings.notify_cooldown_minutes,
            },
            "anomalies": {
                "enabled": settings.notify_anomalies_enabled,
                "low_altitude_ft": settings.anomaly_low_altitude_ft,
                "min_speed_mph": settings.anomaly_min_speed_mph,
                "radius_miles": settings.anomaly_radius_miles,
                "emergency_squawks": ["7500", "7600", "7700"],
            },
        },
    }


def recent_notifications(limit: int = 10) -> list[dict[str, Any]]:
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                created_at,
                observed_at,
                icao24,
                callsign,
                rule,
                title,
                message,
                priority,
                sent_ok,
                error
            FROM notification_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def send_test_notification() -> dict[str, Any]:
    candidate = {
        "rule": "test",
        "title": "Flight tracker test",
        "message": "Phone alerts are connected for Appleton Flight Tracker.",
        "priority": "default",
        "tags": "airplane",
        "observation": {
            "observed_at": int(time.time()),
            "icao24": "test",
            "callsign": "TEST",
        },
        "details": {},
    }
    sent_ok, error = send_ntfy_notification(candidate)
    return {
        "sent_ok": sent_ok,
        "error": error,
        "config": notification_config(),
    }


def get_access_token() -> str | None:
    if not settings.opensky_client_id or not settings.opensky_client_secret:
        return None

    with token_lock:
        now = time.time()
        if token_cache["access_token"] and token_cache["expires_at"] - 60 > now:
            return token_cache["access_token"]

        payload = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": settings.opensky_client_id,
                "client_secret": settings.opensky_client_secret,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            OPEN_SKY_TOKEN_URL,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=settings.opensky_timeout_seconds
        ) as response:
            body = json.loads(response.read().decode("utf-8"))

        token = body.get("access_token")
        expires_in = int(body.get("expires_in", 1800))
        if not token:
            raise RuntimeError("OpenSky token response did not include access_token")

        token_cache["access_token"] = token
        token_cache["expires_at"] = now + expires_in
        return token


def fetch_opensky_states() -> tuple[int | None, dict[str, Any]]:
    bbox = miles_bbox(settings.home_lat, settings.home_lon, settings.search_radius_miles)
    params = urllib.parse.urlencode({**bbox, "extended": 1})
    url = f"{OPEN_SKY_STATES_URL}?{params}"

    headers = {"User-Agent": USER_AGENT}
    token = get_access_token()
    source = "opensky-oauth" if token else "opensky-anonymous"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(
        request, timeout=settings.opensky_timeout_seconds
    ) as response:
        return response.status, {"source": source, **json.loads(response.read())}


def record_api_event(
    ok: bool,
    source: str,
    message: str,
    status_code: int | None = None,
    aircraft_count: int = 0,
) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO api_events
                (created_at, source, ok, status_code, message, aircraft_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(time.time()), source, 1 if ok else 0, status_code, message, aircraft_count),
        )


def persist_observations(observations: list[dict[str, Any]]) -> None:
    if not observations:
        return

    fields = [
        "observed_at",
        "response_time",
        "icao24",
        "callsign",
        "origin_country",
        "longitude",
        "latitude",
        "baro_altitude_m",
        "geo_altitude_m",
        "on_ground",
        "velocity_ms",
        "true_track_deg",
        "vertical_rate_ms",
        "squawk",
        "position_source",
        "category",
        "distance_miles",
        "bearing_deg",
        "overhead",
        "raw_json",
    ]
    placeholders = ", ".join("?" for _ in fields)
    columns = ", ".join(fields)
    values = [[observation[field] for field in fields] for observation in observations]
    with connect_db() as conn:
        conn.executemany(
            f"INSERT OR IGNORE INTO observations ({columns}) VALUES ({placeholders})",
            values,
        )


def purge_below_collection_filter() -> int:
    with connect_db() as conn:
        cursor = conn.execute(
            """
            DELETE FROM observations
            WHERE
                on_ground = 1
                OR velocity_ms IS NULL
                OR velocity_ms < ?
                OR COALESCE(geo_altitude_m, baro_altitude_m) IS NULL
                OR COALESCE(geo_altitude_m, baro_altitude_m) < ?
            """,
            (collection_speed_threshold_ms(), collection_altitude_threshold_m()),
        )
        return cursor.rowcount


def poll_opensky(force: bool = False) -> dict[str, Any]:
    global last_poll_at, last_status

    with poll_lock:
        now = time.time()
        if not force and last_poll_at and now - last_poll_at < settings.poll_interval_seconds:
            return last_status

        status_code = None
        source = "opensky"
        try:
            status_code, payload = fetch_opensky_states()
            source = payload.pop("source", source)
            observed_at = int(payload.get("time") or time.time())
            states = payload.get("states") or []
            observations = [
                observation
                for state in states
                if (observation := state_to_observation(state, observed_at)) is not None
            ]
            persist_observations(observations)
            notifications = process_notifications(observations)
            message = f"Stored {len(observations)} fast/high aircraft observations."
            if notifications:
                message += f" Sent {len(notifications)} notification(s)."
            last_status = {
                "ok": True,
                "message": message,
                "aircraft_count": len(observations),
                "notification_count": len(notifications),
                "source": source,
                "status_code": status_code,
                "at": int(time.time()),
            }
            last_poll_at = time.time()
            record_api_event(True, source, message, status_code, len(observations))
            return last_status

        except urllib.error.HTTPError as exc:
            message = f"OpenSky returned HTTP {exc.code}: {exc.reason}"
            status_code = exc.code
        except urllib.error.URLError as exc:
            message = f"Could not reach OpenSky: {exc.reason}"
        except Exception as exc:  # noqa: BLE001 - keep the local service alive.
            message = f"Polling failed: {exc}"
            traceback.print_exc()

        last_status = {
            "ok": False,
            "message": message,
            "aircraft_count": 0,
            "source": source,
            "status_code": status_code,
            "at": int(time.time()),
        }
        last_poll_at = time.time()
        record_api_event(False, source, message, status_code, 0)
        return last_status


def latest_aircraft(minutes: int = 20) -> list[dict[str, Any]]:
    cutoff = int(time.time()) - minutes * 60
    with connect_db() as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT icao24, MAX(observed_at) AS observed_at
                FROM observations
                WHERE observed_at >= ?
                GROUP BY icao24
            )
            SELECT o.*
            FROM observations o
            JOIN latest l
                ON o.icao24 = l.icao24
               AND o.observed_at = l.observed_at
            ORDER BY o.distance_miles ASC, o.callsign ASC
            """,
            (cutoff,),
        ).fetchall()

    aircraft: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        enrichment = get_enrichment(
            row["icao24"],
            row["callsign"],
            allow_fetch=index < settings.adsbdb_max_enrich_per_refresh,
        )
        aircraft.append(public_aircraft(row, enrichment))
    return aircraft


def history_summary(hours: int) -> dict[str, Any]:
    hours = max(1, min(hours, 24 * 30))
    cutoff = int(time.time()) - hours * 3600
    with connect_db() as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS observations,
                COUNT(DISTINCT icao24) AS aircraft,
                SUM(CASE WHEN overhead = 1 THEN 1 ELSE 0 END) AS overhead_observations,
                MIN(distance_miles) AS closest_distance
            FROM observations
            WHERE observed_at >= ?
            """,
            (cutoff,),
        ).fetchone()
        hourly_rows = conn.execute(
            """
            SELECT
                CAST(observed_at / 3600 AS INTEGER) * 3600 AS bucket,
                COUNT(*) AS observations,
                COUNT(DISTINCT icao24) AS aircraft,
                SUM(CASE WHEN overhead = 1 THEN 1 ELSE 0 END) AS overhead
            FROM observations
            WHERE observed_at >= ?
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (cutoff,),
        ).fetchall()
        common_rows = conn.execute(
            """
            SELECT
                icao24,
                COALESCE(MAX(callsign), '') AS callsign,
                COUNT(*) AS observations,
                MIN(distance_miles) AS closest_distance,
                MAX(observed_at) AS last_seen
            FROM observations
            WHERE observed_at >= ?
            GROUP BY icao24
            ORDER BY observations DESC
            LIMIT 8
            """,
            (cutoff,),
        ).fetchall()
        closest_rows = conn.execute(
            """
            SELECT *
            FROM observations
            WHERE observed_at >= ?
            ORDER BY distance_miles ASC
            LIMIT 8
            """,
            (cutoff,),
        ).fetchall()

    return {
        "hours": hours,
        "totals": {
            "observations": totals["observations"] or 0,
            "aircraft": totals["aircraft"] or 0,
            "overhead_observations": totals["overhead_observations"] or 0,
            "closest_distance": totals["closest_distance"],
        },
        "hourly": [dict(row) for row in hourly_rows],
        "common_aircraft": [dict(row) for row in common_rows],
        "closest_passes": [
            public_aircraft(
                row,
                get_enrichment(row["icao24"], row["callsign"], allow_fetch=False),
            )
            for row in closest_rows
        ],
    }


def aircraft_history(icao24: str, hours: int) -> dict[str, Any]:
    hours = max(1, min(hours, 24 * 30))
    cutoff = int(time.time()) - hours * 3600
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM observations
            WHERE icao24 = ? AND observed_at >= ?
            ORDER BY observed_at ASC
            """,
            (icao24.lower(), cutoff),
        ).fetchall()
    return {
        "icao24": icao24.lower(),
        "hours": hours,
        "points": [
            public_aircraft(
                row,
                get_enrichment(row["icao24"], row["callsign"], allow_fetch=False),
            )
            for row in rows
        ],
    }


def latest_events(limit: int = 5) -> list[dict[str, Any]]:
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM api_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def app_config() -> dict[str, Any]:
    return {
        "label": settings.label,
        "home": {"lat": settings.home_lat, "lon": settings.home_lon},
        "search_radius_miles": settings.search_radius_miles,
        "overhead_radius_miles": settings.overhead_radius_miles,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "bbox": miles_bbox(
            settings.home_lat, settings.home_lon, settings.search_radius_miles
        ),
        "has_opensky_credentials": bool(
            settings.opensky_client_id and settings.opensky_client_secret
        ),
        "adsbdb_enabled": settings.adsbdb_enabled,
        "adsbdb_cache_hours": settings.adsbdb_cache_hours,
        "collection_filter": {
            "min_speed_mph": settings.collect_min_speed_mph,
            "min_altitude_ft": settings.collect_min_altitude_ft,
        },
        "notifications": notification_config(),
    }


def parse_query(path: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urllib.parse.urlparse(path)
    return parsed.path, urllib.parse.parse_qs(parsed.query)


def safe_static_path(request_path: str) -> Path | None:
    path = posixpath.normpath(urllib.parse.unquote(request_path))
    if path in {".", "/"}:
        path = "/index.html"
    if path.startswith("/"):
        path = path[1:]
    candidate = (PUBLIC_DIR / path).resolve()
    try:
        candidate.relative_to(PUBLIC_DIR)
    except ValueError:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if candidate.exists() else None


class FlightTrackerHandler(BaseHTTPRequestHandler):
    server_version = "AppletonFlightTracker/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            f"{self.log_date_time_string()} {self.address_string()} {fmt % args}",
            file=sys.stderr,
        )

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, file_path: Path) -> None:
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_not_found(self) -> None:
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        path, query = parse_query(self.path)

        try:
            if path == "/api/config":
                self.send_json(app_config())
                return

            if path == "/api/aircraft":
                force = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
                poll_status = poll_opensky(force=force)
                self.send_json(
                    {
                        "status": poll_status,
                        "config": app_config(),
                        "aircraft": latest_aircraft(),
                        "events": latest_events(),
                    }
                )
                return

            if path == "/api/history":
                hours = env_int_from_query(query, "hours", 24)
                self.send_json(history_summary(hours))
                return

            if path == "/api/notifications":
                self.send_json(
                    {
                        "config": notification_config(),
                        "notifications": recent_notifications(),
                    }
                )
                return

            if path == "/api/notifications/test":
                self.send_json(send_test_notification())
                return

            if path.startswith("/api/aircraft/") and path.endswith("/history"):
                parts = [part for part in path.split("/") if part]
                if len(parts) == 4:
                    hours = env_int_from_query(query, "hours", 12)
                    self.send_json(aircraft_history(parts[2], hours))
                    return

            static_path = safe_static_path(path)
            if static_path:
                self.send_static(static_path)
                return

            self.send_not_found()

        except Exception as exc:  # noqa: BLE001 - return useful local API errors.
            traceback.print_exc()
            self.send_json(
                {"error": str(exc), "type": exc.__class__.__name__},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def env_int_from_query(query: dict[str, list[str]], name: str, default: int) -> int:
    raw = query.get(name, [str(default)])[0]
    try:
        return int(raw)
    except ValueError:
        return default


def poll_loop() -> None:
    while not shutdown_event.is_set():
        poll_opensky(force=False)
        shutdown_event.wait(settings.poll_interval_seconds)


def main() -> None:
    init_db()
    purged = purge_below_collection_filter()
    if purged:
        print(
            "Purged "
            f"{purged} stored observations below "
            f"{settings.collect_min_speed_mph:g} mph / "
            f"{settings.collect_min_altitude_ft:g} ft."
        )
    threading.Thread(target=poll_loop, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", settings.port), FlightTrackerHandler)
    print(f"Flight tracker running at http://127.0.0.1:{settings.port}")
    print(f"SQLite history: {settings.db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping flight tracker.")
    finally:
        shutdown_event.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
