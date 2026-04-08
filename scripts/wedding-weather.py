#!/usr/bin/env python3
"""
Wedding Weather Monitor — Nottingham Road KZN
Runs daily at 09:00 SAST, sends detailed hourly forecast via Telegram.

Schedule:
  14:50 Walk to ceremony (outdoors)
  15:00-15:45 Ceremony (indoors)
  15:45 Walk back (outdoors)
  16:00-17:45 Canapés (OUTDOORS ← main exposure)
  17:45+ Reception (indoors, protected)

Wedding date: Saturday 11 April 2026

SOURCE HIERARCHY:
  1. YR.no (MET Norway) — PRIMARY, hourly, ECMWF-based, free
  2. Open-Meteo ECMWF — SECONDARY, hourly, direct ECMWF IFS HRES
  3. Windy GFS — BACKUP, hourly, free tier supported
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

import requests

# Constants
WEDDING_DATE = "2026-04-11"
LOCATION_QUERY = "Nottingham Road, KwaZulu-Natal, South Africa"
SAST = timezone(timedelta(hours=2))
TELEGRAM_ID = "7018990694"
FOCUS_START = 14  # walk to ceremony
FOCUS_END = 18    # reception starts 17:45

# Cache file for geocoding result
GEOCACHE_FILE = os.path.join(os.path.dirname(__file__), "wedding-location.json")

# User-Agent for API requests (required by Nominatim and MET Norway)
USER_AGENT = "WeddingWeatherBot/1.0 (Blake's Wedding - Contact: Telegram @Blake_1863)"

# WMO weather code descriptions
WMO = {
    0: ("☀️", "Clear sky"),
    1: ("🌤", "Mainly clear"),
    2: ("⛅", "Partly cloudy"),
    3: ("☁️", "Overcast"),
    45: ("🌫", "Fog"),
    48: ("🌫", "Icy fog"),
    51: ("🌦", "Light drizzle"),
    53: ("🌦", "Drizzle"),
    55: ("🌧", "Heavy drizzle"),
    61: ("🌧", "Light rain"),
    63: ("🌧", "Rain"),
    65: ("🌧", "Heavy rain"),
    80: ("🌦", "Light showers"),
    81: ("🌧", "Showers"),
    82: ("⛈", "Heavy showers"),
    95: ("⛈", "Thunderstorm"),
    96: ("⛈", "Thunderstorm + hail"),
    99: ("⛈", "Thunderstorm + hail"),
}

# Per-hour schedule: (label, is_outdoors)
SCHEDULE = {
    14: ("Walk to ceremony", True),
    15: ("Ceremony (indoors)", False),
    16: ("Canapés OUTDOORS", True),
    17: ("Canapés OUTDOORS", True),
    18: ("Reception (indoors)", False),
}


# ============================================================
# 1. LOCATION RESOLUTION (Nominatim)
# ============================================================

def geocode_location():
    """Resolve location to lat/lon via OpenStreetMap Nominatim."""
    if os.path.exists(GEOCACHE_FILE):
        try:
            with open(GEOCACHE_FILE, "r") as f:
                cached = json.load(f)
                print(f"[INFO] Using cached location: {cached.get('display_name', 'unknown')}")
                return cached
        except Exception as e:
            print(f"[WARN] Cache read failed: {e}, will re-geocode")

    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": LOCATION_QUERY, "format": "jsonv2", "limit": 1}
    headers = {"User-Agent": USER_AGENT}

    print(f"[INFO] Geocoding: {LOCATION_QUERY}")
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    results = r.json()

    if not results:
        params["q"] = "Nottingham Road, KZN, South Africa"
        print(f"[INFO] Retrying with: {params['q']}")
        r = requests.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        results = r.json()

    if not results:
        raise RuntimeError("Geocoding failed: no results for Nottingham Road")

    result = results[0]
    location = {
        "display_name": result["display_name"],
        "lat": round(float(result["lat"]), 4),
        "lon": round(float(result["lon"]), 4),
    }

    os.makedirs(os.path.dirname(GEOCACHE_FILE), exist_ok=True)
    with open(GEOCACHE_FILE, "w") as f:
        json.dump(location, f, indent=2)
    print(f"[INFO] Cached location: {location['display_name']}")

    return location


# ============================================================
# 2. SOURCE A: YR.NO (MET NORWAY) - PRIMARY
# ============================================================

def fetch_yr_no(lat, lon):
    """Fetch forecast from MET Norway Locationforecast API."""
    url = f"https://api.met.no/weatherapi/locationforecast/2.0/complete?lat={lat}&lon={lon}"
    headers = {"User-Agent": USER_AGENT}

    print(f"[INFO] Fetching YR.no forecast for {lat}, {lon}...")
    r = requests.get(url, headers=headers, timeout=45)
    r.raise_for_status()
    return r.json()


def normalize_yr_no(yr_data, lat, lon):
    """Convert YR.no JSON to standardized format."""
    ts = yr_data["properties"]["timeseries"]

    SYMBOL_TO_WMO = {
        "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3,
        "rainshowers": 80, "rainshowersandthunder": 95, "sleetshowers": 80,
        "snowshowers": 80, "rain": 61, "heavyrain": 63, "heavyrainshowers": 81,
        "sleet": 61, "snow": 71, "fog": 45, "thunderstorm": 95,
    }

    hourly_time, hourly_temp, hourly_precip = [], [], []
    hourly_precip_prob, hourly_weathercode, hourly_wind, hourly_gusts = [], [], [], []

    for entry in ts:
        time_str = entry["time"]
        data = entry["data"]
        instant = data["instant"]["details"]

        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        local_str = dt.astimezone(SAST).strftime("%Y-%m-%dT%H:%M:%S")

        hourly_time.append(local_str)
        hourly_temp.append(float(instant.get("air_temperature", 0)))

        precip = 0.0
        if "next_1_hours" in data and data["next_1_hours"]:
            precip = float(data["next_1_hours"].get("details", {}).get("precipitation_amount", 0))
        elif "next_6_hours" in data and data["next_6_hours"]:
            precip = float(data["next_6_hours"].get("details", {}).get("precipitation_amount", 0)) / 6
        hourly_precip.append(precip)

        symbol = "cloudy"
        if "next_1_hours" in data and data["next_1_hours"]:
            symbol = data["next_1_hours"].get("summary", {}).get("symbol_code", "cloudy")
        elif "next_6_hours" in data and data["next_6_hours"]:
            symbol = data["next_6_hours"].get("summary", {}).get("symbol_code", "cloudy")
        symbol_base = symbol.replace("day", "").replace("night", "").replace("_", "")
        hourly_weathercode.append(SYMBOL_TO_WMO.get(symbol_base, 3))

        if "rain" in symbol or "sleet" in symbol or "snow" in symbol:
            hourly_precip_prob.append(70 if "heavy" in symbol else 50)
        elif "showers" in symbol:
            hourly_precip_prob.append(40)
        elif "cloudy" in symbol or "partly" in symbol:
            hourly_precip_prob.append(20)
        else:
            hourly_precip_prob.append(10)

        wind_ms = float(instant.get("wind_speed", 0))
        hourly_wind.append(wind_ms * 3.6)
        hourly_gusts.append(wind_ms * 3.6 * 1.3)

    # Daily aggregates
    daily_data = {}
    for i, t in enumerate(hourly_time):
        date = t.split("T")[0]
        if date not in daily_data:
            daily_data[date] = {"temps": [], "precip": [], "precip_prob": [], "wind": [], "wcodes": []}
        daily_data[date]["temps"].append(hourly_temp[i])
        daily_data[date]["precip"].append(hourly_precip[i])
        daily_data[date]["precip_prob"].append(hourly_precip_prob[i])
        daily_data[date]["wind"].append(hourly_wind[i])
        daily_data[date]["wcodes"].append(hourly_weathercode[i])

    daily_time = sorted(daily_data.keys())
    daily_tmax = [max(daily_data[d]["temps"]) for d in daily_time]
    daily_tmin = [min(daily_data[d]["temps"]) for d in daily_time]
    daily_precip_sum = [sum(daily_data[d]["precip"]) for d in daily_time]
    daily_precip_prob_max = [max(daily_data[d]["precip_prob"]) for d in daily_time]
    daily_weathercode = [max(daily_data[d]["wcodes"]) for d in daily_time]
    daily_wind_max = [max(daily_data[d]["wind"]) for d in daily_time]

    return {
        "source": "yr.no",
        "source_name": "YR.no (MET Norway)",
        "resolution": "hourly (first 2-3 days), 6-hourly thereafter",
        "hourly": {
            "time": hourly_time, "temperature_2m": hourly_temp, "precipitation": hourly_precip,
            "precipitation_probability": hourly_precip_prob, "weathercode": hourly_weathercode,
            "windspeed_10m": hourly_wind, "windgusts_10m": hourly_gusts,
        },
        "daily": {
            "time": daily_time, "temperature_2m_max": daily_tmax, "temperature_2m_min": daily_tmin,
            "precipitation_sum": daily_precip_sum, "precipitation_probability_max": daily_precip_prob_max,
            "weathercode": daily_weathercode, "sunrise": ["06:15"] * len(daily_time),
            "sunset": ["17:50"] * len(daily_time), "windspeed_10m_max": daily_wind_max,
        }
    }


# ============================================================
# 3. SOURCE B: OPEN-METEO ECMWF (SECONDARY)
# ============================================================

def fetch_open_meteo_ecmwf(lat, lon):
    """
    Fetch ECMWF IFS HRES forecast from Open-Meteo.
    Free, no API key, hourly resolution.
    """
    url = (
        f"https://api.open-meteo.com/v1/ecmwf?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,apparent_temperature,precipitation,precipitation_probability,"
        "weathercode,windspeed_10m,windgusts_10m,relativehumidity_2m,cloudcover"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
        "precipitation_probability_max,weathercode,sunrise,sunset,windspeed_10m_max"
        "&timezone=Africa%2FJohannesburg&forecast_days=14"
    )
    print(f"[INFO] Fetching Open-Meteo ECMWF forecast...")
    try:
        r = requests.get(url, timeout=45)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] Open-Meteo ECMWF failed: {e}")
        return None


def normalize_open_meteo(om_data):
    """Convert Open-Meteo ECMWF JSON to standardized format."""
    hourly = om_data.get("hourly", {})
    daily = om_data.get("daily", {})
    
    # Open-Meteo returns arrays with time as first element
    times = hourly.get("time", [])
    
    # Convert times to SAST
    hourly_time = []
    for t in times:
        dt = datetime.fromisoformat(t)
        hourly_time.append(dt.strftime("%Y-%m-%dT%H:%M:%S"))
    
    return {
        "source": "open-meteo-ecmwf",
        "source_name": "Open-Meteo (ECMWF IFS HRES)",
        "resolution": "hourly",
        "hourly": {
            "time": hourly_time,
            "temperature_2m": hourly.get("temperature_2m", []),
            "precipitation": hourly.get("precipitation", []),
            "precipitation_probability": hourly.get("precipitation_probability", []),
            "weathercode": hourly.get("weathercode", []),
            "windspeed_10m": hourly.get("windspeed_10m", []),
            "windgusts_10m": hourly.get("windgusts_10m", []),
        },
        "daily": {
            "time": daily.get("time", []),
            "temperature_2m_max": daily.get("temperature_2m_max", []),
            "temperature_2m_min": daily.get("temperature_2m_min", []),
            "precipitation_sum": daily.get("precipitation_sum", []),
            "precipitation_probability_max": daily.get("precipitation_probability_max", []),
            "weathercode": daily.get("weathercode", []),
            "sunrise": daily.get("sunrise", []),
            "sunset": daily.get("sunset", []),
            "windspeed_10m_max": daily.get("windspeed_10m_max", []),
        }
    }


# ============================================================
# 4. SOURCE C: WINDY GFS (BACKUP)
# ============================================================

def get_windy_api_key():
    """Fetch Windy API key from secrets manager."""
    try:
        result = subprocess.run(
            ["python3", "/home/admin/.openclaw/secrets/secrets.py", "get", "windy_api_key"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        print(f"[WARN] Could not fetch Windy API key: {e}")
    return None


def fetch_windy_gfs(lat, lon, api_key):
    """
    Fetch Windy forecast using GFS model (free tier).
    GFS is available in Windy's free API.
    """
    if not api_key:
        return None
    url = "https://api.windy.com/api/point-forecast/v2"
    payload = {
        "lat": lat,
        "lon": lon,
        "model": "gfs",  # GFS is free
        "levels": ["surface"],
        "parameters": ["temp", "precip", "wind", "windGust"]
    }
    try:
        r = requests.post(url, json=payload, params={"apikey": api_key}, timeout=45)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 400:
            print(f"[WARN] Windy GFS API: invalid request or key")
        else:
            print(f"[WARN] Windy GFS API failed: {r.status_code}")
        return None
    except Exception as e:
        print(f"[WARN] Windy GFS API failed: {e}")
        return None


def normalize_windy_gfs(windy_data):
    """Convert Windy GFS JSON to standardized format."""
    if not windy_data or "data" not in windy_data:
        return None
    
    data = windy_data["data"]
    times = data.get("time", [])
    temps = data.get("temp", [])
    precip = data.get("precip", [])
    wind = data.get("wind", [])
    gusts = data.get("windGust", [])
    
    hourly_time, hourly_temp, hourly_precip = [], [], []
    hourly_precip_prob, hourly_weathercode, hourly_wind, hourly_gusts = [], [], [], []
    
    for i, ts in enumerate(times):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(SAST)
        hourly_time.append(dt.strftime("%Y-%m-%dT%H:%M:%S"))
        hourly_temp.append(float(temps[i]) if i < len(temps) else 0)
        hourly_precip.append(float(precip[i]) if i < len(precip) else 0)
        hourly_precip_prob.append(30 if precip[i] > 0 else 10)
        hourly_weathercode.append(3)
        hourly_wind.append(float(wind[i]) if i < len(wind) else 0)
        hourly_gusts.append(float(gusts[i]) if i < len(gusts) else 0)
    
    # Daily aggregates
    daily_data = {}
    for i, t in enumerate(hourly_time):
        date = t.split("T")[0]
        if date not in daily_data:
            daily_data[date] = {"temps": [], "precip": [], "wind": []}
        daily_data[date]["temps"].append(hourly_temp[i])
        daily_data[date]["precip"].append(hourly_precip[i])
        daily_data[date]["wind"].append(hourly_wind[i])
    
    daily_time = sorted(daily_data.keys())
    
    return {
        "source": "windy-gfs",
        "source_name": "Windy (GFS)",
        "resolution": "hourly",
        "hourly": {
            "time": hourly_time, "temperature_2m": hourly_temp, "precipitation": hourly_precip,
            "precipitation_probability": hourly_precip_prob, "weathercode": hourly_weathercode,
            "windspeed_10m": hourly_wind, "windgusts_10m": hourly_gusts,
        },
        "daily": {
            "time": daily_time,
            "temperature_2m_max": [max(daily_data[d]["temps"]) for d in daily_time],
            "temperature_2m_min": [min(daily_data[d]["temps"]) for d in daily_time],
            "precipitation_sum": [sum(daily_data[d]["precip"]) for d in daily_time],
            "precipitation_probability_max": [50 if any(p > 0 for p in daily_data[d]["precip"]) else 20 for d in daily_time],
            "weathercode": [3] * len(daily_time),
            "sunrise": ["06:15"] * len(daily_time),
            "sunset": ["17:50"] * len(daily_time),
            "windspeed_10m_max": [max(daily_data[d]["wind"]) for d in daily_time],
        }
    }


# ============================================================
# 4. SOURCE C: SAWS (qualitative cross-check only)
# ============================================================

def fetch_saws_warnings():
    """Fetch SAWS weather warnings. Returns status dict (no machine-readable forecast)."""
    url = "https://www.weathersa.co.za/warnings"
    print(f"[INFO] Checking SAWS warnings...")
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, verify=True)
        r.raise_for_status()
        return {"source": "SAWS", "status": "fetched", "html": r.text[:5000]}
    except Exception as e:
        print(f"[WARN] SAWS unavailable: {type(e).__name__}")
        return {"source": "SAWS", "status": "unavailable", "reason": str(e)}


# ============================================================
# 5. RISK SCORING
# ============================================================

def risk_score(wcode, rain_mm, rain_pct, wind_kmh, gusts_kmh):
    """Calculate risk score: 0=perfect, 1=minor, 2=moderate, 3=serious"""
    if wcode >= 95: return 3, "⛈ THUNDERSTORM"
    if wcode in (65, 82) or rain_mm > 5: return 3, "🌧 HEAVY RAIN"
    if wcode in (61, 63, 80, 81) or rain_mm > 2: return 2, "🌧 Rain likely"
    if wcode in (51, 53, 55) or rain_pct > 60: return 2, "🌦 Drizzle/showers"
    if rain_pct > 40 or wcode in (2, 3): return 1, "⛅ Some cloud/chance of showers"
    if wind_kmh > 40 or (gusts_kmh and gusts_kmh > 55): return 2, "💨 Strong winds"
    return 0, "✅ Looking good"


# ============================================================
# 6. MESSAGE BUILDING
# ============================================================

def build_message(data, location, saws_status=None, windy_data=None):
    """Build Telegram message with full hourly detail for wedding day."""
    now_sast = datetime.now(SAST)
    hourly = data["hourly"]
    daily = data["daily"]
    source_name = data.get("source_name", "Unknown")
    resolution = data.get("resolution", "hourly")
    
    # Use Open-Meteo hourly data directly if available (true hourly, not 6-hourly estimates)
    use_full_hourly = source_name == "Open-Meteo (ECMWF IFS HRES)"

    FRIDAY, SATURDAY, SUNDAY = "2026-04-10", "2026-04-11", "2026-04-12"
    day_indices = {}
    for label, date_str in [("friday", FRIDAY), ("saturday", SATURDAY), ("sunday", SUNDAY)]:
        for i, d in enumerate(daily["time"]):
            if d == date_str:
                day_indices[label] = i
                break

    if "saturday" not in day_indices:
        last_date = daily["time"][-1] if daily["time"] else "unknown"
        days_until = (datetime.strptime(WEDDING_DATE, "%Y-%m-%d").replace(tzinfo=SAST) - now_sast).days
        return f"⚠️ Wedding forecast not yet available.\n📅 Wedding: {WEDDING_DATE} ({days_until} days away)\n📊 Latest: {last_date}"

    # Get Windy cross-check data for wedding day
    windy_sat = None
    if windy_data and "saturday" in day_indices:
        for i, d in enumerate(windy_data["daily"]["time"]):
            if d == SATURDAY:
                windy_sat = {
                    "tmax": windy_data["daily"]["temperature_2m_max"][i],
                    "tmin": windy_data["daily"]["temperature_2m_min"][i],
                    "rain": windy_data["daily"]["precipitation_sum"][i],
                    "wind": windy_data["daily"]["windspeed_10m_max"][i],
                }
                break

    # 3-day comparison (primary source)
    lines = [
        f"💍 *Wedding Weather Update — Full Hourly Detail*",
        f"📍 {location.get('display_name', 'Nottingham Road').split(',')[0]}",
        f"🕐 Report: {now_sast.strftime('%d %b %H:%M')} SAST",
        f"📡 Source: {source_name}",
        f"",
        f"*3-Day Comparison:*",
        f"```",
        f"{'Day':<18} {'High':>5} {'Low':>5} {'Rain':>6} {'Wind':>6}",
        f"{'─'*18} {'─'*5} {'─'*5} {'─'*6} {'─'*6}",
    ]

    for label, key in [("Friday 10 Apr", "friday"), ("Saturday 11 Apr 💍", "saturday"), ("Sunday 12 Apr", "sunday")]:
        if key in day_indices:
            idx = day_indices[key]
            tmax, tmin = daily["temperature_2m_max"][idx], daily["temperature_2m_min"][idx]
            rain, wind = daily["precipitation_sum"][idx], daily["windspeed_10m_max"][idx]
            lines.append(f"{label:<18} {tmax:>4.0f}° {tmin:>4.0f}° {rain:>5.1f}mm {wind:>5.0f}km/h")
        else:
            lines.append(f"{label:<18} {'—':>5} {'—':>5} {'—':>6} {'—':>6}")
    lines.append("```")

    # Windy GFS cross-check
    if windy_sat:
        lines.extend([
            f"",
            f"*Windy GFS Cross-Check:*",
            f"```",
            f"Saturday: {windy_sat['tmax']:.0f}°/{windy_sat['tmin']:.0f}°  {windy_sat['rain']:.1f}mm  {windy_sat['wind']:.0f}km/h",
            f"```",
        ])
        # Note discrepancies
        yr_sat = day_indices["saturday"]
        yr_tmax = daily["temperature_2m_max"][yr_sat]
        if abs(windy_sat["tmax"] - yr_tmax) > 3:
            lines.append(f"_⚠️ Note: GFS temp differs by {abs(windy_sat['tmax'] - yr_tmax):.0f}°C from primary_")
        lines.append("")

    # Wedding day detail
    sat_idx = day_indices["saturday"]
    tmax, tmin = daily["temperature_2m_max"][sat_idx], daily["temperature_2m_min"][sat_idx]
    rain_day, rain_pct = daily["precipitation_sum"][sat_idx], daily["precipitation_probability_max"][sat_idx]
    wcode_d = daily["weathercode"][sat_idx]
    wemo, wdesc = WMO.get(wcode_d, ("🌡", f"Code {wcode_d}"))
    wind_max = daily["windspeed_10m_max"][sat_idx]
    days_to_go = (datetime.strptime(WEDDING_DATE, "%Y-%m-%d").replace(tzinfo=SAST) - now_sast).days + 1

    lines.extend([
        f"", f"*Saturday 11 April — Wedding Day:* {days_to_go} days to go",
        f"{wemo} {wdesc}",
        f"🌡 {tmin:.0f}°C – {tmax:.0f}°C  |  🌧 {rain_day:.1f}mm ({rain_pct:.0f}% chance)",
        f"💨 Max wind: {wind_max:.0f} km/h", f"",
    ])

    if "6-hourly" in resolution.lower():
        lines.append(f"_⚠️ Resolution: 6-hourly (hourly values are estimates)_")
        lines.append("")

    if saws_status:
        status = "No severe weather warnings" if saws_status.get("status") == "fetched" else "Unavailable"
        lines.append(f"_🇿🇦 SAWS: {status}_")
        lines.append("")

    # Hourly breakdown — full day detail (06:00-22:00)
    lines.extend([
        f"*⏰ Your Day — Full Hourly Detail:*", f"```",
        f"{'Time':<6} {'°C':>3} {'Feels':>5} {'Rain':>5} {'Prob':>5} {'Wind':>5}  Activity",
        f"{'─'*6} {'─'*3} {'─'*5} {'─'*5} {'─'*5} {'─'*5}  {'─'*22}",
    ])

    canapes_risks = []
    for i, ts in enumerate(hourly["time"]):
        if not ts.startswith(WEDDING_DATE): continue
        hour = int(ts.split("T")[1].split(":")[0])
        if hour < 6 or hour > 22: continue  # Show full day: 06:00-22:00

        temp = hourly["temperature_2m"][i]
        feels = hourly.get("apparent_temperature", hourly["temperature_2m"])[i]
        rain_h, rain_p = hourly["precipitation"][i], hourly["precipitation_probability"][i]
        wc, wind = hourly["weathercode"][i], hourly["windspeed_10m"][i]
        emoji, _ = WMO.get(wc, ("🌡", ""))
        gusts = hourly.get("windgusts_10m", [None] * len(hourly["time"]))[i]
        r, _ = risk_score(wc, rain_h, rain_p, wind, gusts)

        label, is_outdoor = SCHEDULE.get(hour, ("", False))
        if is_outdoor: canapes_risks.append(r)
        marker = f" {label}{' 🌿' if is_outdoor else ' 🏠'}" if label else ""
        lines.append(f"{hour:02d}:00  {temp:>2.0f}°  {feels:>4.0f}°  {rain_h:>4.1f}mm  {rain_p:>4.0f}%  {wind:>4.0f}km  {emoji}{marker}")

    lines.extend(["```", "_🌿 outdoors  🏠 indoors_"])

    # Canapés detailed risk assessment
    canapes_hours = [i for i, ts in enumerate(hourly["time"]) if ts.startswith(WEDDING_DATE) and int(ts.split("T")[1].split(":")[0]) in [16, 17]]
    if canapes_hours:
        max_prob = max(hourly["precipitation_probability"][i] for i in canapes_hours)
        max_rain = max(hourly["precipitation"][i] for i in canapes_hours)
        avg_temp = sum(hourly["temperature_2m"][i] for i in canapes_hours) / len(canapes_hours)
        lines.extend([
            "",
            "*Canapés Window (16:00-17:45):*",
            f"🌡 Avg temp: {avg_temp:.0f}°C  |  🌧 Max rain: {max_rain:.1f}mm  |  ☔ Max prob: {max_prob:.0f}%",
        ])
        if max_prob < 30 and max_rain < 0.5:
            lines.append("✅ *Low risk* — outdoor canapés should be fine!")
        elif max_prob < 60:
            lines.append("🟡 *Mild risk* — have a backup plan ready.")
        else:
            lines.append("🟠 *Moderate risk* — recommend covered area.")
    else:
        canapes_max = max(canapes_risks) if canapes_risks else 0
        msgs = {
            0: "✅ *Canapés window clear!*",
            1: "🟡 *Mild risk* — light cloud possible, probably fine.",
            2: "🟠 *Rain likely* — recommend covered fallback.",
            3: "🔴 *HIGH RISK* — activate wet weather plan.",
        }
        lines.extend(["", f"{msgs[canapes_max]} (16:00–17:45)"])

    lines.extend(["", f"_Next update: tomorrow ~09:00 SAST_"])

    return "\n".join(lines)


# ============================================================
# 7. TELEGRAM DELIVERY
# ============================================================

def send_telegram(message):
    result = subprocess.run(
        ["openclaw", "message", "send", "--channel", "telegram", "--target", TELEGRAM_ID, "--message", message],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"[ERROR] Telegram: {result.stderr}", file=sys.stderr)
        sys.exit(1)


# ============================================================
# 7. MAIN
# ============================================================

def main():
    dry_run = "--dry-run" in sys.argv
    print(f"[{datetime.now(SAST).strftime('%Y-%m-%d %H:%M SAST')}] Wedding weather check...")

    # Step 1: Geocode
    try:
        location = geocode_location()
    except Exception as e:
        print(f"[ERROR] Geocoding failed: {e}")
        location = {"display_name": "Nottingham Road, KZN (fallback)", "lat": -29.35, "lon": 29.98}

    # Step 2: Fetch Open-Meteo ECMWF (PRIMARY - true hourly data)
    om_data = None
    try:
        om_raw = fetch_open_meteo_ecmwf(location["lat"], location["lon"])
        if om_raw:
            om_data = normalize_open_meteo(om_raw)
            print(f"[INFO] ✓ Open-Meteo (ECMWF IFS HRES) fetched")
    except Exception as e:
        print(f"[WARN] Open-Meteo ECMWF failed: {e}")

    # Step 3: Fetch YR.no (SECONDARY - fallback, 6-hourly beyond 48h)
    yr_data = None
    if not om_data:
        print(f"[INFO] Open-Meteo unavailable, trying YR.no...")
    try:
        yr_raw = fetch_yr_no(location["lat"], location["lon"])
        yr_data = normalize_yr_no(yr_raw, location["lat"], location["lon"])
        print(f"[INFO] ✓ YR.no (MET Norway) fetched")
        if not om_data:
            om_data = yr_data  # Use as primary
    except Exception as e:
        print(f"[WARN] YR.no failed: {e}")

    # Step 4: Fetch Windy GFS (BACKUP)
    windy_data = None
    windy_api_key = get_windy_api_key()
    if windy_api_key and om_data:
        windy_raw = fetch_windy_gfs(location["lat"], location["lon"], windy_api_key)
        if windy_raw:
            windy_data = normalize_windy_gfs(windy_raw)
            print(f"[INFO] ✓ Windy (GFS) fetched")

    # Ensure we have at least one source
    if not om_data:
        print(f"[ERROR] All forecast sources failed")
        sys.exit(1)

    # Step 5: Check SAWS (qualitative)
    saws_status = fetch_saws_warnings()

    # Step 6: Build message (use Open-Meteo as primary)
    message = build_message(om_data, location, saws_status, windy_data)
    print(message)

    if not dry_run:
        send_telegram(message)
        print("Sent.")
    else:
        print("[DRY RUN] not sent")


if __name__ == "__main__":
    main()
