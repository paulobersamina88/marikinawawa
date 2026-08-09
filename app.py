import math
import re
import unicodedata
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
import folium
from streamlit_folium import st_folium

st.set_page_config(
    page_title="Marikina River + Upper Wawa Forecast",
    page_icon="🌊",
    layout="wide",
)

# -----------------------------------------------------------------------------
# EXPERIMENTAL DEFAULTS — ALL EDITABLE IN THE SIDEBAR
# -----------------------------------------------------------------------------
TZ = "Asia/Manila"
FORECAST_HOURS = 120
BUILD_ID = "2026-08-09-SPATIAL-RAIN-MUSKINGUM-v6"

# User-requested declared Upper Wawa catchment area used for rainfall-runoff.
WAWA_CATCHMENT_KM2 = 262.0

# Prime Infra describes a ~450 ha reservoir. Here it is used ONLY as an
# approximate surcharge-storage surface area for level routing near FSL.
# This is not an official elevation-storage curve.
WAWA_SURFACE_AREA_KM2 = 4.5
WAWA_FSL_M = 135.00

# Assumed temporary spill curve: Q = K * (H - FSL)^1.5, H > FSL.
# K=430 approximately reproduces ~190 m3/s at H=135.58 m.
SPILL_K = 430.0
SPILL_EXP = 1.5

# Map coordinate for Upper Wawa. Rainfall forecast nodes below are screening
# points used to preserve spatial/temporal rainfall variation. They are not
# official PAGASA rainfall gauges and do not define catchment boundaries.
WAWA_DAM_COORD = (14.70072, 121.20310)

# Open-Meteo rainfall points used as spatial rainfall proxies.
RAIN_POINTS = {
    "Upper Wawa": WAWA_DAM_COORD,
    "San Jose, Antipolo": (14.61944, 121.28194),
    "Tanay, Rizal": (14.49850, 121.28560),
    "Montalban / Rodriguez": (14.7235, 121.1385),
    "Nangka": (14.6830, 121.1085),
    "Marikina": (14.65070, 121.10290),
}

# Downstream gauge coordinates are initial approximate plotting points only.
# They are also used to estimate straight-line reach length for the temporary
# routing model; a user-editable meander factor converts this to an effective
# river length. Replace with surveyed/verified coordinates when available.
STATION_MAP_COORDS = {
    "Montalban": (14.7315, 121.1510),
    "Rodriguez": (14.7160, 121.1260),
    "Nangka": (14.6830, 121.1085),
    "Sto Nino": (14.6507, 121.1029),
    "Tumana Bridge": (14.6715, 121.0965),
}

# Rainfall time series assigned to the incremental catchment beside each reach.
# These weights are temporary spatial proxies and are shown explicitly in the UI.
RAIN_ZONE_LABELS = {
    "Montalban": "Upper Wawa + Montalban/Rodriguez",
    "Rodriguez": "Montalban/Rodriguez",
    "Nangka": "Nangka + San Jose",
    "Sto Nino": "Marikina + Nangka",
    "Tumana Bridge": "Marikina",
}

MAP_STATUS_COLORS = {
    "NORMAL": "green",
    "ALERT": "orange",
    "ALARM": "red",
    "CRITICAL": "darkred",
    "Unknown": "gray",
}

DEFAULT_STATIONS = pd.DataFrame(
    [
        # Current EL stays blank until official PAGASA data or a deliberate
        # manual value is supplied. Hydraulic/routing values below are
        # temporary calibration assumptions, not surveyed river parameters.
        {"station": "Montalban", "current_el_m": None, "alert_el_m": 22.40, "alarm_el_m": 23.00, "critical_el_m": 23.60,
         "wave_speed_kmh": 4.0, "muskingum_x": 0.20, "local_area_km2": 18.0, "local_tc_hr": 2.0, "stage_response_hr": 1.0, "stage_m_per_100cms": 0.11},
        {"station": "Rodriguez", "current_el_m": None, "alert_el_m": 28.80, "alarm_el_m": 29.80, "critical_el_m": 30.70,
         "wave_speed_kmh": 4.0, "muskingum_x": 0.20, "local_area_km2": 15.0, "local_tc_hr": 2.0, "stage_response_hr": 1.0, "stage_m_per_100cms": 0.12},
        {"station": "Nangka", "current_el_m": None, "alert_el_m": 16.50, "alarm_el_m": 17.10, "critical_el_m": 17.70,
         "wave_speed_kmh": 3.5, "muskingum_x": 0.22, "local_area_km2": 35.0, "local_tc_hr": 3.0, "stage_response_hr": 1.5, "stage_m_per_100cms": 0.14},
        {"station": "Sto Nino", "current_el_m": None, "alert_el_m": 15.00, "alarm_el_m": 16.00, "critical_el_m": 17.00,
         "wave_speed_kmh": 3.0, "muskingum_x": 0.25, "local_area_km2": 28.0, "local_tc_hr": 3.0, "stage_response_hr": 1.5, "stage_m_per_100cms": 0.16},
        {"station": "Tumana Bridge", "current_el_m": None, "alert_el_m": 17.26, "alarm_el_m": 18.26, "critical_el_m": 19.26,
         "wave_speed_kmh": 2.5, "muskingum_x": 0.25, "local_area_km2": 18.0, "local_tc_hr": 2.0, "stage_response_hr": 1.5, "stage_m_per_100cms": 0.18},
    ]
)

PAGASA_WATER_URL = "https://pasig-marikina-tullahanffws.pagasa.dost.gov.ph/water/map.do"
PAGASA_WATER_TABLE_URL = "https://pasig-marikina-tullahanffws.pagasa.dost.gov.ph/water/table.do"
PAGASA_MAIN_URL = "https://pasig-marikina-tullahanffws.pagasa.dost.gov.ph/main.do"
PAGASA_RAIN_URL = "https://pasig-marikina-tullahanffws.pagasa.dost.gov.ph/rainfall/map.do"
LIVE_LEVEL_TTL_SECONDS = 300

TARGET_STATIONS = ["Montalban", "Rodriguez", "Nangka", "Sto Nino", "Tumana Bridge"]


# -----------------------------------------------------------------------------
# LIVE PAGASA RIVER-LEVEL INGESTION
# -----------------------------------------------------------------------------
def _norm_text(value) -> str:
    """Normalize station labels so Sto. Niño / Sto Nino and punctuation match."""
    value = "" if value is None else str(value)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace(".", " ").replace("-", " ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def canonical_station_name(value):
    t = _norm_text(value)
    if "tumana" in t:
        return "Tumana Bridge"
    if "sto nino" in t:
        return "Sto Nino"
    if t == "montalban" or " montalban " in f" {t} ":
        return "Montalban"
    if t == "rodriguez" or " rodriguez " in f" {t} ":
        return "Rodriguez"
    if t == "nangka" or " nangka " in f" {t} ":
        return "Nangka"
    return None


def _number(value):
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s or s in {"-", "--", "No Data", "No Data."}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _parse_pagasa_timestamp(page_text):
    m = re.search(r"Time\s*:\s*(20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2})", page_text, flags=re.I)
    if not m:
        return None
    raw = m.group(1).replace("/", "-")
    try:
        ts = pd.Timestamp(raw)
        return ts.tz_localize(TZ) if ts.tzinfo is None else ts.tz_convert(TZ)
    except Exception:
        return None


def _age_minutes(ts):
    if ts is None or pd.isna(ts):
        return None
    try:
        now_local = pd.Timestamp.now(tz=TZ)
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize(TZ)
        else:
            t = t.tz_convert(TZ)
        return max(0.0, (now_local - t).total_seconds() / 60.0)
    except Exception:
        return None


def _trend_from_values(current, previous):
    if current is None or previous is None:
        return "Unknown"
    d = float(current) - float(previous)
    if d > 0.005:
        return "Rising"
    if d < -0.005:
        return "Falling"
    return "Stable"


def parse_pagasa_water_html(html: str, source_url: str) -> pd.DataFrame:
    """Parse PAGASA server-rendered water-level tables when data are present."""
    soup = BeautifulSoup(html or "", "html.parser")
    page_text = soup.get_text(" ", strip=True)
    observed_at = _parse_pagasa_timestamp(page_text)
    rows = []

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        station = canonical_station_name(cells[0])
        if station not in TARGET_STATIONS:
            continue

        vals = cells[1:]
        current = minus30 = minus1h = minus2h = alert = alarm = critical = None
        # PAGASA /water/table.do: station, current, -30m, -1h, -2h, alert, alarm, critical
        if len(vals) >= 7:
            current, minus30, minus1h, minus2h, alert, alarm, critical = [_number(v) for v in vals[:7]]
        # PAGASA /water/map.do: station, current, alert, alarm, critical
        elif len(vals) >= 4:
            current, alert, alarm, critical = [_number(v) for v in vals[:4]]
        else:
            continue

        if current is None:
            continue
        delta_1h = (current - minus1h) if minus1h is not None else None
        rate_1h = delta_1h
        trend = _trend_from_values(current, minus30 if minus30 is not None else minus1h)
        rows.append({
            "station": station,
            "live_el_m": current,
            "minus30_el_m": minus30,
            "minus1h_el_m": minus1h,
            "minus2h_el_m": minus2h,
            "delta_1h_m": delta_1h,
            "rate_m_per_hr": rate_1h,
            "alert_el_m_live": alert,
            "alarm_el_m_live": alarm,
            "critical_el_m_live": critical,
            "trend": trend,
            "observed_at": observed_at,
            "data_age_min": _age_minutes(observed_at),
            "source": "PAGASA FFWS direct",
            "quality": "Official direct",
            "estimated": False,
            "source_url": source_url,
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset=["station"], keep="first")


def parse_pagasa_pasted_table(text: str) -> pd.DataFrame:
    """Parse text copied directly from PAGASA /water/map.do.

    Expected columns: Station, Current EL, Alert EL, Alarm EL, Critical EL.
    Asterisks and (*) flags are tolerated. Only the five Marikina-model
    stations are retained.
    """
    text = text or ""
    observed_at = _parse_pagasa_timestamp(text)
    rows = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("*").strip()
        if not line:
            continue
        # The PAGASA table copies cleanly as tab-separated text. Also accept
        # two-or-more spaces for users copying through other browsers.
        parts = [p.strip().strip("*").strip() for p in re.split(r"\t+|\s*\|\s*|\s{2,}", line) if p.strip()]
        if len(parts) < 2:
            continue
        station = canonical_station_name(parts[0])
        if station not in TARGET_STATIONS:
            continue
        current = _number(parts[1] if len(parts) > 1 else None)
        alert = _number(parts[2] if len(parts) > 2 else None)
        alarm = _number(parts[3] if len(parts) > 3 else None)
        critical = _number(parts[4] if len(parts) > 4 else None)
        if current is None:
            continue
        rows.append({
            "station": station,
            "live_el_m": current,
            "minus30_el_m": None,
            "minus1h_el_m": None,
            "minus2h_el_m": None,
            "delta_1h_m": None,
            "rate_m_per_hr": None,
            "alert_el_m_live": alert,
            "alarm_el_m_live": alarm,
            "critical_el_m_live": critical,
            "trend": "Unknown",
            "observed_at": observed_at,
            "data_age_min": _age_minutes(observed_at),
            "source": "PAGASA Water Level Map — pasted table",
            "quality": "Official PAGASA table paste",
            "estimated": False,
            "source_url": PAGASA_WATER_URL,
        })
    return pd.DataFrame(rows).drop_duplicates(subset=["station"], keep="first") if rows else pd.DataFrame()


def _pagasa_session_get(session: requests.Session, url: str, timeout: int = 20) -> str:
    """Browser-like request to the official PAGASA Java web application."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-PH,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://pasig-marikina-tullahanffws.pagasa.dost.gov.ph/main.do",
    }
    r = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text


@st.cache_data(ttl=LIVE_LEVEL_TTL_SECONDS, show_spinner=False)
def fetch_live_river_levels():
    """Fetch Current EL from the user's authoritative PAGASA Water Level Map.

    /water/map.do is authoritative for Current/Alert/Alarm/Critical. The
    official /water/table.do page is used only to enrich -30 min / -1 h / -2 h
    trend fields when it is available; it never replaces the map Current EL.
    No third-party river-level fallback is used.
    """
    errors = []
    map_df = pd.DataFrame()
    table_df = pd.DataFrame()
    session = requests.Session()

    # Warm the Java web session first. Failure here is non-fatal.
    try:
        _pagasa_session_get(session, PAGASA_MAIN_URL, timeout=12)
    except Exception as exc:
        errors.append(f"Session warm-up: {exc}")

    try:
        html = _pagasa_session_get(session, PAGASA_WATER_URL)
        map_df = parse_pagasa_water_html(html, PAGASA_WATER_URL)
        if map_df.empty:
            errors.append("PAGASA Water Level Map loaded but returned no usable station rows.")
        else:
            map_df["source"] = "PAGASA Water Level Map"
            map_df["quality"] = "Official PAGASA direct"
    except Exception as exc:
        errors.append(f"{PAGASA_WATER_URL}: {exc}")

    # Optional official history enrichment; map values remain authoritative.
    try:
        html_hist = _pagasa_session_get(session, PAGASA_WATER_TABLE_URL)
        table_df = parse_pagasa_water_html(html_hist, PAGASA_WATER_TABLE_URL)
    except Exception as exc:
        errors.append(f"History table: {exc}")

    result = map_df.copy()
    if not result.empty and not table_df.empty:
        hist_cols = [
            "station", "minus30_el_m", "minus1h_el_m", "minus2h_el_m",
            "delta_1h_m", "rate_m_per_hr", "trend"
        ]
        hist = table_df[hist_cols].drop_duplicates("station")
        result = result.drop(columns=[c for c in hist_cols[1:] if c in result.columns], errors="ignore")
        result = result.merge(hist, on="station", how="left")
        # Recalculate trend against the map Current EL, not the table Current EL.
        result["delta_1h_m"] = result.apply(
            lambda r: float(r["live_el_m"]) - float(r["minus1h_el_m"])
            if pd.notna(r.get("minus1h_el_m")) else None,
            axis=1,
        )
        result["rate_m_per_hr"] = result["delta_1h_m"]
        result["trend"] = result.apply(
            lambda r: _trend_from_values(
                r.get("live_el_m"),
                r.get("minus30_el_m") if pd.notna(r.get("minus30_el_m")) else r.get("minus1h_el_m")
            ), axis=1
        )

    if not result.empty:
        result = result.drop_duplicates(subset=["station"], keep="first")
        result["data_age_min"] = pd.to_numeric(result.get("data_age_min"), errors="coerce")

    meta = {
        "errors": errors,
        "direct_count": len(result) if not result.empty else 0,
        "fetched_at": pd.Timestamp.now(tz=TZ),
        "authoritative_url": PAGASA_WATER_URL,
    }
    return result, meta


def seed_station_inputs_from_live(defaults: pd.DataFrame, live: pd.DataFrame) -> pd.DataFrame:
    """Seed Current EL and thresholds from live feed while preserving manual-edit capability."""
    seeded = defaults.copy()
    if live is None or live.empty:
        return seeded
    live_by_station = live.set_index("station")
    for idx, row in seeded.iterrows():
        station = row["station"]
        if station not in live_by_station.index:
            continue
        lrow = live_by_station.loc[station]
        live_el = _number(lrow.get("live_el_m"))
        if live_el is not None:
            seeded.at[idx, "current_el_m"] = live_el
        for source_col, target_col in [
            ("alert_el_m_live", "alert_el_m"),
            ("alarm_el_m_live", "alarm_el_m"),
            ("critical_el_m_live", "critical_el_m"),
        ]:
            val = _number(lrow.get(source_col))
            if val is not None:
                seeded.at[idx, target_col] = val
    return seeded


# -----------------------------------------------------------------------------
# DATA FUNCTIONS
# -----------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def fetch_open_meteo_hourly(lat: float, lon: float, past_days: int = 3, forecast_days: int = 6) -> pd.DataFrame:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation,rain,showers",
        "timezone": TZ,
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json().get("hourly", {})
    df = pd.DataFrame({
        "time": pd.to_datetime(data.get("time", []), errors="coerce"),
        "precipitation": pd.to_numeric(pd.Series(data.get("precipitation", [])), errors="coerce"),
        "rain": pd.to_numeric(pd.Series(data.get("rain", [])), errors="coerce"),
        "showers": pd.to_numeric(pd.Series(data.get("showers", [])), errors="coerce"),
    })
    if df.empty:
        raise ValueError("Open-Meteo returned no hourly data")
    # Open-Meteo returns local clock time because timezone=Asia/Manila.
    df["time"] = df["time"].dt.tz_localize(TZ, nonexistent="shift_forward", ambiguous="NaT")
    df["rain_mm"] = df[["precipitation", "rain", "showers"]].max(axis=1).fillna(0.0)
    return df[["time", "rain_mm"]].dropna(subset=["time"]).reset_index(drop=True)


def build_rainfall_dataset() -> pd.DataFrame:
    """Fetch point rainfall and preserve separate hourly forcing by river zone."""
    series = {}
    for name, (lat, lon) in RAIN_POINTS.items():
        df = fetch_open_meteo_hourly(lat, lon)
        series[name] = df.set_index("time")["rain_mm"].rename(name)
    merged = pd.concat(series.values(), axis=1).sort_index().fillna(0.0)

    # Upper Wawa basin proxy. The 262 km² runoff calculation uses this series.
    merged["wawa_proxy_mm"] = (
        merged["Upper Wawa"] * 0.50
        + merged["San Jose, Antipolo"] * 0.35
        + merged["Tanay, Rizal"] * 0.15
    )

    # Incremental rainfall forcing for each downstream reach. These are kept as
    # separate hourly time series instead of one basin-wide average so a storm
    # can move from the mountains toward Marikina and create multiple peaks.
    merged["rain_montalban_mm"] = (
        merged["Upper Wawa"] * 0.50 + merged["Montalban / Rodriguez"] * 0.50
    )
    merged["rain_rodriguez_mm"] = merged["Montalban / Rodriguez"]
    merged["rain_nangka_mm"] = (
        merged["Nangka"] * 0.65 + merged["San Jose, Antipolo"] * 0.35
    )
    merged["rain_stonino_mm"] = (
        merged["Marikina"] * 0.65 + merged["Nangka"] * 0.35
    )
    merged["rain_tumana_mm"] = merged["Marikina"]
    return merged.reset_index()


def runoff_coefficient(antecedent_48h_mm: float, offset: float = 0.0) -> float:
    if antecedent_48h_mm < 20:
        c = 0.35
    elif antecedent_48h_mm < 50:
        c = 0.45
    elif antecedent_48h_mm < 100:
        c = 0.60
    elif antecedent_48h_mm < 150:
        c = 0.75
    else:
        c = 0.85
    return min(max(c + offset, 0.20), 0.95)


def spill_discharge(level_m: float, fsl_m: float, k: float, exponent: float) -> float:
    head = max(float(level_m) - float(fsl_m), 0.0)
    return float(k) * (head ** float(exponent)) if head > 0 else 0.0


def lag_runoff(raw_q: pd.Series, weights) -> pd.Series:
    result = pd.Series(0.0, index=raw_q.index)
    for lag, weight in enumerate(weights):
        result = result + raw_q.shift(lag, fill_value=0.0) * float(weight)
    return result


def haversine_km(a, b) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(x), math.sqrt(max(1 - x, 0.0)))


def muskingum_route(inflow: pd.Series, k_hr: float, x: float, initial_outflow: float | None = None) -> pd.Series:
    """Hourly Muskingum routing for one river reach.

    K is travel/storage time in hours and X is the storage weighting factor.
    A stability floor is applied to K for the fixed 1-hour model time step.
    """
    qin = pd.Series(inflow, dtype=float).fillna(0.0).clip(lower=0.0).reset_index(drop=True)
    if qin.empty:
        return qin
    dt = 1.0
    x = min(max(float(x), 0.0), 0.49)
    min_k = dt / max(2.0 * (1.0 - x), 0.02)
    k = max(float(k_hr), min_k + 1e-6)
    den = k - k * x + 0.5 * dt
    c0 = (-k * x + 0.5 * dt) / den
    c1 = (k * x + 0.5 * dt) / den
    c2 = (k - k * x - 0.5 * dt) / den

    out = [float(qin.iloc[0] if initial_outflow is None else initial_outflow)]
    for i in range(1, len(qin)):
        q = c0 * float(qin.iloc[i]) + c1 * float(qin.iloc[i - 1]) + c2 * float(out[-1])
        out.append(max(q, 0.0))
    return pd.Series(out, index=qin.index)


def linear_reservoir_route(raw_q: pd.Series, tau_hr: float, initial_state: float = 0.0) -> pd.Series:
    """Simple runoff translation/storage for an incremental subcatchment."""
    tau = max(float(tau_hr), 0.25)
    alpha = 1.0 - math.exp(-1.0 / tau)
    state = max(float(initial_state), 0.0)
    out = []
    for q in pd.Series(raw_q).fillna(0.0):
        state = state + alpha * (max(float(q), 0.0) - state)
        out.append(max(state, 0.0))
    return pd.Series(out, index=pd.Series(raw_q).index)


def zone_rain_column(station: str) -> str:
    return {
        "Montalban": "rain_montalban_mm",
        "Rodriguez": "rain_rodriguez_mm",
        "Nangka": "rain_nangka_mm",
        "Sto Nino": "rain_stonino_mm",
        "Tumana Bridge": "rain_tumana_mm",
    }[station]


def local_runoff_hydrograph(
    past_df: pd.DataFrame,
    future_df: pd.DataFrame,
    rain_col: str,
    area_km2: float,
    tc_hr: float,
    rain_factor: float = 1.0,
    c_offset: float = 0.0,
):
    """Create a future local-runoff hydrograph with antecedent wetness and recession."""
    hist = past_df[["time", rain_col]].copy() if not past_df.empty else pd.DataFrame(columns=["time", rain_col])
    fut = future_df[["time", rain_col]].copy()
    combo = pd.concat([hist, fut], ignore_index=True).sort_values("time").reset_index(drop=True)
    combo["rain_scaled"] = pd.to_numeric(combo[rain_col], errors="coerce").fillna(0.0) * float(rain_factor)
    combo["ant48"] = combo["rain_scaled"].rolling(48, min_periods=1).sum()
    combo["runoff_c"] = combo["ant48"].apply(lambda x: runoff_coefficient(float(x), c_offset))
    combo["raw_local_cms"] = 0.278 * combo["runoff_c"] * combo["rain_scaled"] * max(float(area_km2), 0.0)
    combo["local_cms"] = linear_reservoir_route(combo["raw_local_cms"], tau_hr=tc_hr, initial_state=0.0)
    n_hist = len(hist)
    baseline = float(combo["local_cms"].iloc[n_hist - 1]) if n_hist > 0 else 0.0
    future = combo.iloc[n_hist:].copy().reset_index(drop=True)
    return future["local_cms"].reset_index(drop=True), baseline, future


def route_reservoir(
    forecast_df: pd.DataFrame,
    initial_level_m: float,
    abstraction_mld: float,
    env_flow_cms: float,
    catchment_km2: float,
    surface_area_km2: float,
    fsl_m: float,
    spill_k: float,
    spill_exp: float,
    base_inflow_cms: float,
    rain_multiplier: float = 1.0,
    c_offset: float = 0.0,
) -> pd.DataFrame:
    work = forecast_df.copy().reset_index(drop=True)
    work["wawa_rain_mm"] = work["wawa_proxy_mm"] * rain_multiplier
    work["antecedent_48h_mm"] = work["wawa_rain_mm"].rolling(48, min_periods=1).sum()
    work["runoff_c"] = work["antecedent_48h_mm"].apply(lambda x: runoff_coefficient(x, c_offset))

    # Rational-method form for hourly rainfall intensity:
    # Q [m3/s] = 0.278 C I[mm/hr] A[km2]
    work["raw_runoff_cms"] = 0.278 * work["runoff_c"] * work["wawa_rain_mm"] * catchment_km2
    work["rain_inflow_cms"] = lag_runoff(work["raw_runoff_cms"], [0.20, 0.45, 0.25, 0.10])
    work["total_inflow_cms"] = work["rain_inflow_cms"] + float(base_inflow_cms)

    abstraction_cms = float(abstraction_mld) / 86.4
    level = float(initial_level_m)
    area_m2 = max(float(surface_area_km2), 0.01) * 1_000_000.0

    levels = []
    spills = []
    downstream_out = []

    # 12 substeps/hour improves stability of the simplified level-pool routing.
    substeps = 12
    dt = 3600.0 / substeps

    for _, row in work.iterrows():
        qin = max(float(row["total_inflow_cms"]), 0.0)
        spill_sum = 0.0
        out_sum = 0.0
        for _ in range(substeps):
            qspill = spill_discharge(level, fsl_m, spill_k, spill_exp)
            # Environmental flow is an outflow from storage but still continues downstream.
            q_storage_out = qspill + abstraction_cms + float(env_flow_cms)
            ds = (qin - q_storage_out) * dt
            level += ds / area_m2
            # Avoid meaningless negative elevations if the simplified model is stress-tested.
            level = max(level, fsl_m - 10.0)
            spill_sum += qspill
            out_sum += qspill + float(env_flow_cms)
        levels.append(level)
        spills.append(spill_sum / substeps)
        downstream_out.append(out_sum / substeps)

    work["wawa_level_m"] = levels
    work["spill_cms"] = spills
    work["wawa_downstream_cms"] = downstream_out
    work["abstraction_cms"] = abstraction_cms
    return work


def local_rain_memory(rain_series: pd.Series, decay: float = 0.82, initial_memory: float = 0.0) -> pd.Series:
    """Exponential wetness/runoff-memory index.

    Unlike the earlier version, this can be seeded from antecedent rainfall.
    Therefore the future index can DECREASE when rainfall becomes lighter or stops.
    """
    vals = []
    memory = max(float(initial_memory), 0.0)
    for rain in rain_series.fillna(0.0):
        memory = memory * float(decay) + float(rain)
        vals.append(memory)
    return pd.Series(vals, index=rain_series.index)


def local_memory_seed(past_rain_series: pd.Series, decay: float = 0.82) -> float:
    memory = 0.0
    for rain in pd.Series(past_rain_series).fillna(0.0):
        memory = memory * float(decay) + float(rain)
    return float(memory)


def first_order_stage_response(target_delta: pd.Series, tau_hr: float) -> pd.Series:
    """Route a target stage anomaly through a first-order storage/recession response.

    This gives the forecast a rising AND falling limb. A long tau represents more
    channel/floodplain storage and slower recession; a short tau responds faster.
    """
    tau = max(float(tau_hr), 0.25)
    alpha = 1.0 - math.exp(-1.0 / tau)
    state = 0.0
    out = []
    for target in pd.Series(target_delta).fillna(0.0):
        state = state + alpha * (float(target) - state)
        out.append(state)
    return pd.Series(out, index=target_delta.index)


def stage_status(stage, alert, alarm, critical):
    try:
        s = float(stage)
        a = float(alert)
        al = float(alarm)
        c = float(critical)
    except Exception:
        return "Unknown"
    if s >= c:
        return "CRITICAL"
    if s >= al:
        return "ALARM"
    if s >= a:
        return "ALERT"
    return "NORMAL"


def build_downstream_stage_forecast(
    sim_df: pd.DataFrame,
    stations: pd.DataFrame,
    past48: pd.DataFrame,
    rain_factor: float = 1.0,
    c_offset: float = 0.0,
    meander_factor: float = 1.30,
) -> pd.DataFrame:
    """Semi-distributed rainfall-runoff + sequential river routing model.

    For each reach:
      1) Route the complete upstream hydrograph with Muskingum.
      2) Generate incremental local runoff from that reach's OWN hourly rainfall.
      3) Add the local runoff to the routed upstream discharge.
      4) Pass that total discharge to the next downstream reach.
      5) Convert CHANGE from the present estimated flow condition to stage change.

    This preserves storm timing from Wawa to Marikina and allows rises, recessions,
    secondary peaks, and overlap between upstream and local runoff hydrographs.
    """
    if sim_df.empty:
        return pd.DataFrame()

    out = []
    order = ["Montalban", "Rodriguez", "Nangka", "Sto Nino", "Tumana Bridge"]
    station_lookup = stations.set_index("station")

    # Current Wawa outflow is already reflected in the observed river stages;
    # stage forecasts therefore respond to change from the current boundary.
    upstream_q = sim_df["wawa_downstream_cms"].reset_index(drop=True).astype(float)
    upstream_baseline_q = float(sim_df.get("initial_wawa_downstream_cms", sim_df["wawa_downstream_cms"]).iloc[0])
    upstream_coord = WAWA_DAM_COORD

    for station_name in order:
        if station_name not in station_lookup.index:
            continue
        srow = station_lookup.loc[station_name]
        target_coord = STATION_MAP_COORDS[station_name]

        straight_km = haversine_km(upstream_coord, target_coord)
        effective_length_km = straight_km * max(float(meander_factor), 1.0)
        wave_speed = max(float(srow.get("wave_speed_kmh", 3.0)), 0.2)
        k_hr_raw = effective_length_km / wave_speed
        x = float(srow.get("muskingum_x", 0.20))
        min_k = 1.0 / max(2.0 * (1.0 - min(max(x, 0.0), 0.49)), 0.02)
        k_hr = max(k_hr_raw, min_k + 1e-6)

        routed_upstream = muskingum_route(
            upstream_q,
            k_hr=k_hr,
            x=x,
            initial_outflow=upstream_baseline_q,
        )

        rain_col = zone_rain_column(station_name)
        local_q, baseline_local_q, local_detail = local_runoff_hydrograph(
            past_df=past48,
            future_df=sim_df,
            rain_col=rain_col,
            area_km2=float(srow.get("local_area_km2", 0.0)),
            tc_hr=float(srow.get("local_tc_hr", 2.0)),
            rain_factor=rain_factor,
            c_offset=c_offset,
        )

        total_q = routed_upstream.reset_index(drop=True) + local_q.reset_index(drop=True)
        baseline_total_q = float(upstream_baseline_q) + float(baseline_local_q)

        flow_sensitivity = float(srow.get("stage_m_per_100cms", 0.10))
        upstream_target_delta = ((routed_upstream - float(upstream_baseline_q)) / 100.0) * flow_sensitivity
        local_target_delta = ((local_q - float(baseline_local_q)) / 100.0) * flow_sensitivity
        total_target_delta = upstream_target_delta + local_target_delta

        tau_hr = float(srow.get("stage_response_hr", 1.0))
        upstream_stage = first_order_stage_response(upstream_target_delta, tau_hr=tau_hr)
        local_stage = first_order_stage_response(local_target_delta, tau_hr=tau_hr)
        stage_delta = upstream_stage + local_stage
        stage = float(srow["current_el_m"]) + stage_delta

        temp = pd.DataFrame({
            "time": sim_df["time"].values,
            "station": station_name,
            "predicted_el_m": stage,
            "change_m": stage_delta,
            "rise_m": stage_delta,
            "upstream_stage_change_m": upstream_stage,
            "local_stage_change_m": local_stage,
            # Backward-compatible names for existing plots. In v6, 'wawa' here
            # means the COMPLETE routed upstream contribution, including local
            # runoff that entered at earlier reaches.
            "wawa_rise_m": upstream_stage,
            "local_rain_rise_m": local_stage,
            "routed_upstream_cms": routed_upstream,
            "local_runoff_cms": local_q,
            "total_station_cms": total_q,
            "baseline_station_cms": baseline_total_q,
            "reach_straight_km": straight_km,
            "reach_effective_km": effective_length_km,
            "muskingum_k_hr": k_hr,
            "muskingum_x": x,
            "rain_zone": RAIN_ZONE_LABELS.get(station_name, rain_col),
            "rain_mm": pd.to_numeric(sim_df[rain_col], errors="coerce").fillna(0.0).values,
            "local_runoff_c": local_detail["runoff_c"].values,
            "alert_el_m": srow["alert_el_m"],
            "alarm_el_m": srow["alarm_el_m"],
            "critical_el_m": srow["critical_el_m"],
        })
        temp["status"] = temp.apply(
            lambda r: stage_status(r["predicted_el_m"], r["alert_el_m"], r["alarm_el_m"], r["critical_el_m"]),
            axis=1,
        )
        out.append(temp)

        # CRITICAL: pass the ENTIRE station discharge downstream. Therefore local
        # rain added at Montalban/Rodriguez later becomes upstream inflow to Nangka,
        # Sto Nino and Tumana after subsequent reach routing.
        upstream_q = total_q.reset_index(drop=True)
        upstream_baseline_q = baseline_total_q
        upstream_coord = target_coord

    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def scenario_summary(stage_df: pd.DataFrame, stations: pd.DataFrame, scenario_name: str) -> pd.DataFrame:
    rows = []
    for _, srow in stations.iterrows():
        part = stage_df[stage_df["station"] == srow["station"]].copy()
        if part.empty:
            continue
        peak_idx = part["predicted_el_m"].idxmax()
        low_idx = part["predicted_el_m"].idxmin()
        peak = part.loc[peak_idx]
        low = part.loc[low_idx]
        current = float(srow["current_el_m"])
        rows.append({
            "scenario": scenario_name,
            "station": srow["station"],
            "current_el_m": current,
            "peak_el_m": round(float(peak["predicted_el_m"]), 2),
            "predicted_rise_m": round(float(peak["predicted_el_m"]) - current, 2),
            "minimum_el_m": round(float(low["predicted_el_m"]), 2),
            "predicted_fall_m": round(float(low["predicted_el_m"]) - current, 2),
            "peak_time": pd.Timestamp(peak["time"]).strftime("%b %d, %I:%M %p"),
            "minimum_time": pd.Timestamp(low["time"]).strftime("%b %d, %I:%M %p"),
            "peak_status": peak["status"],
        })
    return pd.DataFrame(rows)


def build_forecast_map(
    operational: pd.DataFrame,
    stations: pd.DataFrame,
    forecast: pd.DataFrame,
    current_wawa_el: float,
    fsl_m: float,
    initial_spill: float,
    peak_wawa_el: float,
    peak_spill_cms: float,
    peak_spill_time,
):
    """Create a FloodWatch-style interactive map for Wawa-to-Marikina monitoring."""
    m = folium.Map(
        location=[14.67, 121.145],
        zoom_start=11,
        tiles="OpenStreetMap",
        control_scale=True,
    )

    # Schematic river/gauge connection. This is a monitoring-path visualization,
    # not a surveyed river centerline.
    route_points = [WAWA_DAM_COORD]
    for station_name in ["Montalban", "Rodriguez", "Nangka", "Sto Nino", "Tumana Bridge"]:
        if station_name in STATION_MAP_COORDS:
            route_points.append(STATION_MAP_COORDS[station_name])
    folium.PolyLine(
        route_points,
        color="#2563eb",
        weight=4,
        opacity=0.60,
        dash_array="8,6",
        tooltip="Upper Wawa → downstream monitoring sequence (schematic)",
    ).add_to(m)

    dam_group = folium.FeatureGroup(name="Upper Wawa Dam", show=True)
    spill_state = "SPILLING" if float(current_wawa_el) >= float(fsl_m) else "BELOW FSL"
    dam_popup = f"""
    <div style='font-size:13px;min-width:255px'>
      <b>Upper Wawa Dam</b><br>
      State: <b>{spill_state}</b><br>
      Current EL: <b>{current_wawa_el:.2f} m</b><br>
      Spill crest/FSL: {fsl_m:.2f} m<br>
      Assumed current spill: <b>{initial_spill:.0f} m³/s</b><br>
      Likely max EL: <b>{peak_wawa_el:.2f} m</b><br>
      Likely max spill: <b>{peak_spill_cms:.0f} m³/s</b><br>
      Max-spill timing: <b>{pd.Timestamp(peak_spill_time).strftime('%b %d, %I:%M %p')} PHT</b><br>
      <small>Spill discharge is from the app's assumed temporary rating curve.</small>
    </div>
    """
    folium.Marker(
        location=list(WAWA_DAM_COORD),
        tooltip=f"Upper Wawa | {spill_state} | {initial_spill:.0f} m³/s",
        popup=folium.Popup(dam_popup, max_width=360),
        icon=folium.Icon(color="blue", icon="tint", prefix="fa"),
    ).add_to(dam_group)
    dam_group.add_to(m)

    station_group = folium.FeatureGroup(name="Forecast River Stations", show=True)
    meta = stations.set_index("station")
    for _, row in operational.iterrows():
        name = row["station"]
        if name not in STATION_MAP_COORDS or name not in meta.index:
            continue
        sm = meta.loc[name]
        status = str(row.get("likely_status", "Unknown"))
        color = MAP_STATUS_COLORS.get(status, "gray")
        rise = float(row.get("likely_rise_m", 0.0) or 0.0)
        fall = float(row.get("likely_fall_m", 0.0) or 0.0)
        change_mag = max(abs(rise), abs(fall))
        radius = 8 + min(change_mag * 9.0, 22.0)

        popup = f"""
        <div style='font-size:13px;min-width:280px'>
          <b>{name}</b><br>
          Likely status: <b>{status}</b><br>
          Current EL: <b>{float(row['current_el_m']):.2f} m</b><br>
          Peak change: <b>{rise:+.2f} m</b><br>
          Lowest change: <b>{fall:+.2f} m</b><br>
          Low peak: {float(row['low_peak_m']):.2f} m<br>
          Likely peak: <b>{float(row['likely_peak_m']):.2f} m</b><br>
          High peak: {float(row['high_peak_m']):.2f} m<br>
          Likely peak time: <b>{row['likely_peak_time']} PHT</b><br><br>
          Alert: {float(sm['alert_el_m']):.2f} m<br>
          Alarm: {float(sm['alarm_el_m']):.2f} m<br>
          Critical: {float(sm['critical_el_m']):.2f} m<br>
          <small>Marker size represents forecast change magnitude; color represents likely peak status.</small>
        </div>
        """
        folium.CircleMarker(
            location=list(STATION_MAP_COORDS[name]),
            radius=radius,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.65,
            weight=3,
            tooltip=(
                f"{name} | {status} | current {float(row['current_el_m']):.2f} m → "
                f"likely peak {float(row['likely_peak_m']):.2f} m ({rise:+.2f} m)"
            ),
            popup=folium.Popup(popup, max_width=390),
        ).add_to(station_group)
    station_group.add_to(m)

    rain_group = folium.FeatureGroup(name="120h Rainfall Nodes", show=True)
    for area, coords in RAIN_POINTS.items():
        total = float(forecast[area].sum()) if area in forecast.columns else 0.0
        peak = float(forecast[area].max()) if area in forecast.columns else 0.0
        if area in forecast.columns and len(forecast):
            idx = forecast[area].idxmax()
            peak_time = pd.Timestamp(forecast.loc[idx, "time"]).strftime("%b %d, %I:%M %p")
        else:
            peak_time = "No data"
        rr = 7 + min(total / 12.0, 18.0)
        popup = f"""
        <div style='font-size:13px;min-width:230px'>
          <b>{area} rainfall node</b><br>
          Next 120h total: <b>{total:.1f} mm</b><br>
          Peak hourly rain: <b>{peak:.1f} mm/h</b><br>
          Peak forecast time: {peak_time} PHT<br>
          <small>Open-Meteo point forecast; not an official PAGASA gauge.</small>
        </div>
        """
        folium.CircleMarker(
            location=list(coords),
            radius=rr,
            color="cadetblue",
            fill=True,
            fill_color="cadetblue",
            fill_opacity=0.25,
            weight=2,
            tooltip=f"{area} | 120h rain {total:.1f} mm | peak {peak:.1f} mm/h",
            popup=folium.Popup(popup, max_width=330),
        ).add_to(rain_group)
    rain_group.add_to(m)

    legend = """
    <div style="position: fixed; bottom: 35px; left: 35px; z-index: 9999;
                background: white; border: 2px solid #94a3b8; border-radius: 8px;
                padding: 10px 12px; font-size: 12px; box-shadow: 0 1px 5px rgba(0,0,0,.2);">
      <b>Likely peak status</b><br>
      <span style='color:green'>●</span> Normal &nbsp;
      <span style='color:orange'>●</span> Alert<br>
      <span style='color:red'>●</span> Alarm &nbsp;
      <span style='color:darkred'>●</span> Critical<br>
      <span style='color:cadetblue'>●</span> Rainfall node<br>
      <small>Station marker size = forecast change magnitude</small>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend))
    folium.LayerControl(collapsed=False).add_to(m)

    all_points = [WAWA_DAM_COORD] + list(STATION_MAP_COORDS.values()) + list(RAIN_POINTS.values())
    m.fit_bounds([[min(p[0] for p in all_points), min(p[1] for p in all_points)],
                  [max(p[0] for p in all_points), max(p[1] for p in all_points)]], padding=(25, 25))
    return m


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------
st.title("🌊 Marikina River + Upper Wawa 120-Hour Forecast")
st.caption(
    "Experimental semi-distributed model: hourly spatial rainfall → 262 km² Wawa runoff/reservoir → sequential Muskingum river routing + incremental local runoff → downstream Marikina stage forecast."
)

st.warning(
    "This is NOT an official flood forecast. The spill rating curve, effective reach lengths, wave speeds, Muskingum parameters, incremental catchment areas, and flow-to-stage sensitivities are temporary calibration assumptions. "
    "Use PAGASA/local warnings as the authoritative basis for public safety decisions."
)

with st.sidebar:
    st.header("Upper Wawa — Current State")
    current_wawa_el = st.number_input("Current Wawa reservoir EL (m)", value=135.58, step=0.01, format="%.2f")
    abstraction_mld = st.number_input("Water-supply abstraction (MLD)", min_value=0.0, max_value=710.0, value=30.0, step=10.0)
    env_flow_cms = st.number_input("Environmental/downstream base release (m³/s)", min_value=0.0, value=1.0, step=0.5)
    base_inflow_cms = st.number_input("Non-rain/base Wawa inflow (m³/s)", min_value=0.0, value=15.0, step=5.0)

    st.divider()
    st.header("Model Geometry")
    catchment_km2 = st.number_input("Upper Wawa catchment area (km²)", min_value=1.0, value=WAWA_CATCHMENT_KM2, step=1.0)
    surface_area_km2 = st.number_input("Approx. reservoir surface area (km²)", min_value=0.1, value=WAWA_SURFACE_AREA_KM2, step=0.1)
    fsl_m = st.number_input("Full supply / spill crest EL (m)", value=WAWA_FSL_M, step=0.01, format="%.2f")
    meander_factor = st.number_input(
        "River-length factor vs straight-line map distance",
        min_value=1.0, max_value=3.0, value=1.30, step=0.05,
        help="Temporary routing geometry factor. Effective reach length = map distance × this factor. Calibrate later with surveyed river length.",
    )

    st.divider()
    st.header("Assumed Spill Curve")
    spill_k = st.number_input("Spill coefficient K", min_value=1.0, value=SPILL_K, step=10.0)
    spill_exp = st.number_input("Spill exponent", min_value=1.0, max_value=2.5, value=SPILL_EXP, step=0.05)
    st.latex(r"Q_{spill}=K(H-H_{FSL})^{n}")

    st.divider()
    st.header("Scenario Uncertainty")
    low_rain_factor = st.slider("Low scenario rainfall factor", 0.40, 1.00, 0.70, 0.05)
    high_rain_factor = st.slider("High scenario rainfall factor", 1.00, 1.80, 1.30, 0.05)
    high_c_offset = st.slider("High scenario runoff-C addition", 0.00, 0.20, 0.10, 0.01)

    st.divider()
    st.header("Official River-Level Feed")
    st.caption("Authoritative Current EL source: PAGASA Pasig-Marikina-Tullahan FFWS Water Level Map. No third-party river-level fallback is used.")
    st.caption("River levels cache for 5 minutes; Open-Meteo rainfall cache for 10 minutes.")

    refresh = st.button("Refresh official PAGASA + rainfall now", use_container_width=True)
    if refresh:
        st.cache_data.clear()
        st.rerun()

    st.link_button("Open official PAGASA Water Level Map", PAGASA_WATER_URL, use_container_width=True)
    st.link_button("Open PAGASA Rainfall", PAGASA_RAIN_URL, use_container_width=True)


# Live station data + editable model inputs
st.subheader("1) PAGASA River Levels + Paste Fallback + Temporary Calibration")
st.caption(
    f"Build: {BUILD_ID} • Authoritative river-level reference: {PAGASA_WATER_URL}"
)
st.caption(
    "The app tries the official PAGASA Water Level Map automatically. If that request fails or returns No Data, "
    "the app does not substitute old sample river levels. Instead, paste the table copied from the same PAGASA page below."
)

# Try the official page first. A failure is treated as an availability condition,
# not as a model error, because the user can paste the exact official table below.
try:
    direct_levels, live_meta = fetch_live_river_levels()
except Exception as exc:
    direct_levels = pd.DataFrame()
    live_meta = {
        "errors": [str(exc)],
        "direct_count": 0,
        "fetched_at": pd.Timestamp.now(tz=TZ),
        "authoritative_url": PAGASA_WATER_URL,
    }

auto_ok = direct_levels is not None and not direct_levels.empty
if auto_ok:
    st.success(
        f"Automatic PAGASA read succeeded: {len(direct_levels)}/{len(TARGET_STATIONS)} model stations found. "
        "You may leave the paste box blank."
    )
else:
    st.warning(
        "Automatic PAGASA extraction is unavailable right now. This is not treated as a model failure. "
        "Open the official PAGASA Water Level Map, copy its table, and paste it in the blank field below."
    )

st.link_button(
    "Open official PAGASA Water Level Map to copy values",
    PAGASA_WATER_URL,
    use_container_width=True,
)

pagasa_paste = st.text_area(
    "Paste PAGASA Water Level table here",
    value="",
    height=210,
    placeholder=(
        "Paste the full table exactly as copied from PAGASA. Example:\n\n"
        "Angono\t12.15(*)\t-\t-\t-*\n"
        "Burgos\t28.54\t27.40\t27.90\t28.40\n"
        "Montalban\t27.04(*)\t22.40\t23.00\t23.60\n"
        "Nangka\t22.21(*)\t16.50\t17.10\t17.70\n"
        "Rodriguez\t29.84\t28.80\t29.80\t30.70\n"
        "Sto Nino\t17.22\t15.00\t16.00\t17.00\n"
        "Tumana Bridge\t11.97\t17.26\t18.26\t19.26"
    ),
    help=(
        "You may paste the ENTIRE PAGASA table. The parser accepts tabs, repeated spaces, (*) flags, and leading/trailing asterisks. "
        "Stations outside this Marikina model are ignored automatically."
    ),
    key="pagasa_manual_paste",
)

pasted_levels = parse_pagasa_pasted_table(pagasa_paste) if pagasa_paste.strip() else pd.DataFrame()
paste_ok = pasted_levels is not None and not pasted_levels.empty

# Preview what the parser understood BEFORE those values enter the hydraulic model.
if pagasa_paste.strip():
    if paste_ok:
        parsed_names = set(pasted_levels["station"].tolist())
        missing_from_paste = [s for s in TARGET_STATIONS if s not in parsed_names]
        preview = pasted_levels[[
            "station", "live_el_m", "alert_el_m_live", "alarm_el_m_live", "critical_el_m_live"
        ]].copy()
        st.markdown("**Parsed PAGASA values**")
        st.dataframe(
            preview.rename(columns={
                "station": "Station",
                "live_el_m": "Current EL (m)",
                "alert_el_m_live": "Alert EL",
                "alarm_el_m_live": "Alarm EL",
                "critical_el_m_live": "Critical EL",
            }),
            use_container_width=True,
            hide_index=True,
        )
        if missing_from_paste:
            st.warning(
                "The paste was read, but these model stations were not found: " + ", ".join(missing_from_paste) + ". "
                "You can repaste the complete official table or fill the missing Current EL manually below."
            )
        else:
            st.success("Paste validated: all five Marikina-model stations were found.")
    else:
        st.warning(
            "Text was pasted, but no Marikina-model station rows could be parsed. "
            "Copy the rows directly from the PAGASA Water Level Map and paste them without reformatting."
        )

# Normal rule requested by the user:
#   1) automatic official PAGASA values when available;
#   2) otherwise use the user's pasted official PAGASA table;
#   3) never use stale sample Current EL values.
# If automatic values exist, an explicit checkbox allows a fresh browser copy to
# override them when the user knows the browser table is newer.
use_paste_override = False
if auto_ok and paste_ok:
    use_paste_override = st.checkbox(
        "Use my pasted PAGASA table instead of the automatic read for this run",
        value=False,
        help="Enable this if the table visible in your browser is newer than the server response received by Streamlit.",
    )

if auto_ok and not use_paste_override:
    live_levels = direct_levels.copy()
    source_mode = "Automatic official PAGASA"
elif paste_ok:
    live_levels = pasted_levels.copy()
    source_mode = "Official PAGASA table — manual paste"
else:
    live_levels = pd.DataFrame()
    source_mode = "Waiting for PAGASA data"

station_seed = seed_station_inputs_from_live(DEFAULT_STATIONS, live_levels)

if live_levels is not None and not live_levels.empty:
    live_display = live_levels.copy()
    threshold_lookup = station_seed.set_index("station")
    statuses = []
    for _, lr in live_display.iterrows():
        sname = lr["station"]
        if sname in threshold_lookup.index:
            sm = threshold_lookup.loc[sname]
            statuses.append(stage_status(lr["live_el_m"], sm["alert_el_m"], sm["alarm_el_m"], sm["critical_el_m"]))
        else:
            statuses.append("Unknown")
    live_display["status"] = statuses
    live_display["observed"] = live_display["observed_at"].apply(
        lambda x: pd.Timestamp(x).strftime("%b %d, %I:%M %p") if x is not None and not pd.isna(x) else "Not included"
    )
    live_display["age_min"] = live_display["data_age_min"].apply(
        lambda x: round(float(x), 0) if x is not None and not pd.isna(x) else None
    )
    live_display["delta_1h_m"] = pd.to_numeric(live_display["delta_1h_m"], errors="coerce").round(2)
    live_display["rate_m_per_hr"] = pd.to_numeric(live_display["rate_m_per_hr"], errors="coerce").round(2)

    s1, s2, s3 = st.columns(3)
    s1.metric("Stations loaded", f"{len(live_display)}/{len(TARGET_STATIONS)}")
    s2.metric("Data source", source_mode)
    s3.metric("Automatic fetch", "Available" if auto_ok else "Unavailable")

    if source_mode == "Official PAGASA table — manual paste":
        st.info(
            "The hydraulic forecast is using the values you pasted from the official PAGASA Water Level Map. "
            "Because the copied map table does not normally include observation history, Δ1h/rate may remain blank."
        )

    show_cols = [
        "station", "live_el_m", "status", "delta_1h_m", "rate_m_per_hr", "trend",
        "observed", "age_min", "quality", "source"
    ]
    st.dataframe(
        live_display[show_cols].rename(columns={
            "station": "Station",
            "live_el_m": "Current EL (m)",
            "status": "Current status",
            "delta_1h_m": "Δ1h (m)",
            "rate_m_per_hr": "Rate (m/hr)",
            "trend": "Trend",
            "observed": "Observed",
            "age_min": "Age (min)",
            "quality": "Quality",
            "source": "Source",
        }),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info(
        "No river level has entered the forecast model yet. Paste the current official PAGASA table above. "
        "The 120-hour river-stage forecast will start only after all required Current EL values are available."
    )

if live_meta.get("errors"):
    with st.expander("Automatic PAGASA fetch diagnostics (optional)"):
        st.caption("These messages only explain why the automatic read may have failed; manual PAGASA paste remains valid.")
        for err in live_meta["errors"]:
            st.code(err)

st.caption(
    "Calibration coefficients below remain experimental. Current EL/thresholds come from PAGASA; routing uses map-derived reach length × the sidebar river-length factor, editable wave speed, Muskingum X, local contributing area, runoff response time, and stage sensitivity."
)
stations = st.data_editor(
    station_seed,
    use_container_width=True,
    hide_index=True,
    num_rows="fixed",
    column_config={
        "station": st.column_config.TextColumn("Station", disabled=True),
        "current_el_m": st.column_config.NumberColumn("Model Current EL (m)", format="%.2f"),
        "alert_el_m": st.column_config.NumberColumn("Alert EL", format="%.2f"),
        "alarm_el_m": st.column_config.NumberColumn("Alarm EL", format="%.2f"),
        "critical_el_m": st.column_config.NumberColumn("Critical EL", format="%.2f"),
        "wave_speed_kmh": st.column_config.NumberColumn("Flood-wave speed (km/h)", min_value=0.2, max_value=20.0, step=0.1, format="%.1f"),
        "muskingum_x": st.column_config.NumberColumn("Muskingum X", min_value=0.0, max_value=0.49, step=0.01, format="%.2f"),
        "local_area_km2": st.column_config.NumberColumn("Incremental local area (km²)", min_value=0.0, max_value=500.0, step=1.0, format="%.1f"),
        "local_tc_hr": st.column_config.NumberColumn("Local runoff response (hr)", min_value=0.25, max_value=24.0, step=0.25, format="%.2f"),
        "stage_response_hr": st.column_config.NumberColumn("Stage response (hr)", min_value=0.25, max_value=12.0, step=0.25, format="%.2f"),
        "stage_m_per_100cms": st.column_config.NumberColumn("Stage m / +100 m³/s", min_value=0.01, max_value=1.0, step=0.01, format="%.2f"),
    },
)

# Ensure numeric fields survive Streamlit editor typing.
numeric_cols = [c for c in stations.columns if c != "station"]
for c in numeric_cols:
    stations[c] = pd.to_numeric(stations[c], errors="coerce")

missing_current = stations.loc[stations["current_el_m"].isna(), "station"].tolist()
if missing_current:
    st.warning(
        "Waiting for Current EL for: " + ", ".join(missing_current) + ". "
        "Paste the official PAGASA table above (recommended) or deliberately enter the missing EL in the calibration table."
    )
    st.stop()

# Fetch rainfall
try:
    rainfall_all = build_rainfall_dataset()
except Exception as e:
    st.error(f"Rainfall forecast could not be downloaded: {e}")
    st.stop()

now = pd.Timestamp.now(tz=TZ).floor("h")
forecast = rainfall_all[(rainfall_all["time"] >= now) & (rainfall_all["time"] < now + pd.Timedelta(hours=FORECAST_HOURS))].copy()
# Include prior 48h rain for antecedent wetness calculation, then pass the future-only frame.
past48 = rainfall_all[(rainfall_all["time"] >= now - pd.Timedelta(hours=48)) & (rainfall_all["time"] < now)].copy()
antecedent_wawa_mm = float(past48["wawa_proxy_mm"].sum()) if not past48.empty else 0.0

if forecast.empty:
    st.error("No future hourly rainfall data is available for the 120-hour forecast window.")
    st.stop()

# Scenario runner. Wawa runoff uses the 262 km² basin proxy; downstream reaches
# keep their own rainfall time series and add local runoff before routing onward.
def run_scenario(name, rain_factor, c_offset):
    future = forecast.copy().reset_index(drop=True)

    # Seed Wawa runoff coefficient with the real/estimated previous 48 h rainfall.
    all_rain = pd.concat([
        past48[["time", "wawa_proxy_mm"]],
        future[["time", "wawa_proxy_mm"]],
    ], ignore_index=True)
    all_rain["scaled"] = all_rain["wawa_proxy_mm"] * rain_factor
    all_rain["ant48"] = all_rain["scaled"].rolling(48, min_periods=1).sum()
    ant_future = all_rain.tail(len(future))["ant48"].reset_index(drop=True)

    custom = future.copy()
    custom["wawa_rain_mm"] = custom["wawa_proxy_mm"] * rain_factor
    custom["antecedent_48h_mm"] = ant_future
    custom["runoff_c"] = custom["antecedent_48h_mm"].apply(lambda x: runoff_coefficient(x, c_offset))
    custom["raw_runoff_cms"] = 0.278 * custom["runoff_c"] * custom["wawa_rain_mm"] * catchment_km2

    # Short Wawa catchment translation using three hours of prehistory so the
    # first forecast hours retain runoff already moving toward the reservoir.
    if not past48.empty:
        hist = past48[["wawa_proxy_mm"]].copy().reset_index(drop=True)
        hist["scaled"] = hist["wawa_proxy_mm"] * rain_factor
        hist["ant48"] = hist["scaled"].rolling(48, min_periods=1).sum()
        hist["c"] = hist["ant48"].apply(lambda x: runoff_coefficient(x, c_offset))
        raw_hist = 0.278 * hist["c"] * hist["scaled"] * catchment_km2
    else:
        raw_hist = pd.Series(dtype=float)
    raw_all = pd.concat([raw_hist.tail(3), custom["raw_runoff_cms"]], ignore_index=True)
    lagged_all = lag_runoff(raw_all, [0.20, 0.45, 0.25, 0.10])
    custom["rain_inflow_override"] = lagged_all.tail(len(custom)).reset_index(drop=True)

    # Upper Wawa level-pool routing with the temporary ungated spill curve.
    custom["total_inflow_cms"] = custom["rain_inflow_override"] + float(base_inflow_cms)
    abstraction_cms = float(abstraction_mld) / 86.4
    level = float(current_wawa_el)
    area_m2 = max(float(surface_area_km2), 0.01) * 1_000_000.0
    levels, spills, downs = [], [], []
    substeps = 12
    dt = 3600.0 / substeps
    for _, row in custom.iterrows():
        qin = max(float(row["total_inflow_cms"]), 0.0)
        qs, qo = 0.0, 0.0
        for _ in range(substeps):
            qspill = spill_discharge(level, fsl_m, spill_k, spill_exp)
            qstorage_out = qspill + abstraction_cms + float(env_flow_cms)
            level += ((qin - qstorage_out) * dt) / area_m2
            level = max(level, fsl_m - 10.0)
            qs += qspill
            qo += qspill + float(env_flow_cms)
        levels.append(level)
        spills.append(qs / substeps)
        downs.append(qo / substeps)
    custom["wawa_level_m"] = levels
    custom["spill_cms"] = spills
    custom["wawa_downstream_cms"] = downs
    custom["abstraction_cms"] = abstraction_cms
    custom["initial_wawa_downstream_cms"] = spill_discharge(current_wawa_el, fsl_m, spill_k, spill_exp) + float(env_flow_cms)
    custom["scenario"] = name

    stage = build_downstream_stage_forecast(
        custom,
        stations,
        past48=past48,
        rain_factor=rain_factor,
        c_offset=c_offset,
        meander_factor=meander_factor,
    )
    stage["scenario"] = name
    return custom, stage


base_sim, base_stage = run_scenario("Likely", 1.00, 0.00)
low_sim, low_stage = run_scenario("Low rain", low_rain_factor, -0.05)
high_sim, high_stage = run_scenario("High rain", high_rain_factor, high_c_offset)

# -----------------------------------------------------------------------------
# DASHBOARD
# -----------------------------------------------------------------------------
st.subheader("2) 5-Day Spatial Rainfall Time Series")
zone_cols = {
    "Upper Wawa basin": "wawa_proxy_mm",
    "Wawa→Montalban": "rain_montalban_mm",
    "Montalban→Rodriguez": "rain_rodriguez_mm",
    "Rodriguez→Nangka": "rain_nangka_mm",
    "Nangka→Sto Nino": "rain_stonino_mm",
    "Sto Nino→Tumana": "rain_tumana_mm",
}
forecast_total_wawa = float(forecast["wawa_proxy_mm"].sum())
peak_idx = forecast["wawa_proxy_mm"].idxmax()
peak_rain = forecast.loc[peak_idx]
zone_totals = {label: float(forecast[col].sum()) for label, col in zone_cols.items()}
max_zone = max(zone_totals, key=zone_totals.get)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Past 48h Wawa-proxy rain", f"{antecedent_wawa_mm:.1f} mm")
m2.metric("Next 120h Wawa-proxy rain", f"{forecast_total_wawa:.1f} mm")
m3.metric("Wettest 120h river zone", max_zone, f"{zone_totals[max_zone]:.1f} mm")
m4.metric("Peak Wawa-proxy hour", f"{float(peak_rain['wawa_proxy_mm']):.1f} mm/h")
st.caption(
    "Each reach keeps its own hourly rainfall forcing. A rain peak near Wawa, Montalban, Nangka or Marikina can therefore arrive at different times and combine with the routed upstream flood wave."
)

spatial_chart = forecast.set_index("time")[[col for col in zone_cols.values()]].copy()
spatial_chart = spatial_chart.rename(columns={v: k for k, v in zone_cols.items()})
st.line_chart(spatial_chart, height=340)

# Operational timing summary for the spatial rainfall fields.
timing_rows = []
for label, col in zone_cols.items():
    ser = pd.to_numeric(forecast[col], errors="coerce").fillna(0.0)
    idx = ser.idxmax()
    wet = forecast.loc[ser >= 0.1, ["time", col]]
    timing_rows.append({
        "Rainfall zone": label,
        "120h total (mm)": round(float(ser.sum()), 1),
        "Peak (mm/h)": round(float(ser.max()), 1),
        "Peak time": pd.Timestamp(forecast.loc[idx, "time"]).strftime("%b %d, %I:%M %p") if len(ser) else "",
        "First rain ≥0.1": pd.Timestamp(wet["time"].iloc[0]).strftime("%b %d, %I:%M %p") if not wet.empty else "None",
    })
st.dataframe(pd.DataFrame(timing_rows), use_container_width=True, hide_index=True)

with st.expander("Show 120-hour spatial rainfall table"):
    rain_table = forecast[["time"] + list(RAIN_POINTS.keys()) + list(zone_cols.values())].copy()
    rain_table["time"] = rain_table["time"].dt.strftime("%Y-%m-%d %H:%M")
    st.dataframe(rain_table.round(2), use_container_width=True, hide_index=True)

st.subheader("3) Upper Wawa Reservoir + Spill Simulation")
initial_spill = spill_discharge(current_wawa_el, fsl_m, spill_k, spill_exp)
peak_spill_idx = base_sim["spill_cms"].idxmax()
peak_spill_row = base_sim.loc[peak_spill_idx]
peak_level_idx = base_sim["wawa_level_m"].idxmax()
peak_level_row = base_sim.loc[peak_level_idx]

w1, w2, w3, w4, w5 = st.columns(5)
w1.metric("Assumed current spill", f"{initial_spill:.0f} m³/s")
w2.metric("Likely max spill", f"{float(peak_spill_row['spill_cms']):.0f} m³/s")
w3.metric("Likely max Wawa EL", f"{float(peak_level_row['wawa_level_m']):.2f} m")
w4.metric("Max forecast inflow", f"{float(base_sim['total_inflow_cms'].max()):.0f} m³/s")
w5.metric("Abstraction", f"{float(base_sim['abstraction_cms'].iloc[0]):.2f} m³/s")
st.caption(
    f"Likely max spill timing: {pd.Timestamp(peak_spill_row['time']).strftime('%b %d, %I:%M %p')} PHT | "
    f"Likely max Wawa EL timing: {pd.Timestamp(peak_level_row['time']).strftime('%b %d, %I:%M %p')} PHT"
)

wawa_chart = base_sim.set_index("time")[["total_inflow_cms", "spill_cms", "wawa_downstream_cms"]]
st.line_chart(wawa_chart, height=320)

level_chart = base_sim.set_index("time")[["wawa_level_m"]]
st.line_chart(level_chart, height=230)

with st.expander("Show Wawa hourly simulation"):
    tbl = base_sim[["time", "wawa_rain_mm", "antecedent_48h_mm", "runoff_c", "raw_runoff_cms", "total_inflow_cms", "wawa_level_m", "spill_cms", "wawa_downstream_cms"]].copy()
    tbl["time"] = tbl["time"].dt.strftime("%Y-%m-%d %H:%M")
    st.dataframe(tbl.round(3), use_container_width=True, hide_index=True)

st.subheader("4) Experimental Downstream River-Level Forecast")
st.caption(
    "Each station receives the complete hydrograph routed from the reach above PLUS runoff generated by that reach's own hourly rainfall. "
    "The combined discharge is then routed to the next station. This allows delayed peaks, attenuation, recession, and multiple/overlapping flood waves. "
    "Muskingum K/X, effective reach length, incremental areas and stage sensitivities remain temporary until calibrated against actual PAGASA hydrographs."
)

summary = pd.concat([
    scenario_summary(low_stage, stations, "Low rain"),
    scenario_summary(base_stage, stations, "Likely"),
    scenario_summary(high_stage, stations, "High rain"),
], ignore_index=True)

# Pivot into one row/station for a fast operational view.
likely_sum = summary[summary["scenario"] == "Likely"].set_index("station")
low_sum = summary[summary["scenario"] == "Low rain"].set_index("station")
high_sum = summary[summary["scenario"] == "High rain"].set_index("station")
rows = []
for station in stations["station"]:
    if station not in likely_sum.index:
        continue
    rows.append({
        "station": station,
        "current_el_m": likely_sum.loc[station, "current_el_m"],
        "low_peak_m": low_sum.loc[station, "peak_el_m"],
        "likely_peak_m": likely_sum.loc[station, "peak_el_m"],
        "high_peak_m": high_sum.loc[station, "peak_el_m"],
        "likely_rise_m": likely_sum.loc[station, "predicted_rise_m"],
        "likely_minimum_m": likely_sum.loc[station, "minimum_el_m"],
        "likely_fall_m": likely_sum.loc[station, "predicted_fall_m"],
        "likely_peak_time": likely_sum.loc[station, "peak_time"],
        "likely_minimum_time": likely_sum.loc[station, "minimum_time"],
        "likely_status": likely_sum.loc[station, "peak_status"],
    })
operational = pd.DataFrame(rows)
st.dataframe(operational, use_container_width=True, hide_index=True)

# Show the actual sequential routing parameters being used in this run.
routing_view = (
    base_stage.sort_values("time")
    .groupby("station", as_index=False)
    .first()[["station", "reach_straight_km", "reach_effective_km", "muskingum_k_hr", "muskingum_x", "rain_zone"]]
)
routing_view = routing_view.merge(
    stations[["station", "wave_speed_kmh", "local_area_km2", "local_tc_hr"]],
    on="station", how="left"
)
st.markdown("**Sequential routing used in the likely scenario**")
st.dataframe(
    routing_view.rename(columns={
        "station": "Downstream station",
        "reach_straight_km": "Map distance (km)",
        "reach_effective_km": "Effective river length (km)",
        "wave_speed_kmh": "Wave speed (km/h)",
        "muskingum_k_hr": "Muskingum K (h)",
        "muskingum_x": "Muskingum X",
        "local_area_km2": "Incremental area (km²)",
        "local_tc_hr": "Local runoff response (h)",
        "rain_zone": "Local rainfall forcing",
    }).round(2),
    use_container_width=True, hide_index=True,
)

st.subheader("5) Upper Wawa → Marikina Forecast Map")
st.caption(
    "Interactive monitoring map: river-station color = likely peak status; marker size = magnitude of forecast peak change. "
    "Rainfall nodes and Upper Wawa can be switched on/off in the layer control."
)
forecast_map = build_forecast_map(
    operational=operational,
    stations=stations,
    forecast=forecast,
    current_wawa_el=current_wawa_el,
    fsl_m=fsl_m,
    initial_spill=initial_spill,
    peak_wawa_el=float(peak_level_row["wawa_level_m"]),
    peak_spill_cms=float(peak_spill_row["spill_cms"]),
    peak_spill_time=peak_spill_row["time"],
)
st_folium(forecast_map, width=None, height=610, returned_objects=[])
st.caption(
    "Map/routing note: downstream station coordinates are still approximate. In v6 they are used to estimate straight-line reach length, then multiplied by the editable river-length factor. "
    "Therefore verified gauge coordinates and surveyed river lengths should replace these temporary geometry inputs during calibration."
)

selected_station = st.selectbox("Station hydrograph", stations["station"].tolist(), index=min(3, len(stations)-1))
station_meta = stations[stations["station"] == selected_station].iloc[0]

chart_parts = []
for name, sdf in [("Low rain", low_stage), ("Likely", base_stage), ("High rain", high_stage)]:
    p = sdf[sdf["station"] == selected_station][["time", "predicted_el_m"]].copy()
    p = p.rename(columns={"predicted_el_m": name}).set_index("time")
    chart_parts.append(p)
stage_chart = pd.concat(chart_parts, axis=1)
stage_chart["Alert"] = float(station_meta["alert_el_m"])
stage_chart["Alarm"] = float(station_meta["alarm_el_m"])
stage_chart["Critical"] = float(station_meta["critical_el_m"])
st.line_chart(stage_chart, height=380)

# Decompose likely stage change into the complete routed-upstream contribution
# and the rainfall/runoff generated in the selected station's incremental reach.
selected_likely = base_stage[base_stage["station"] == selected_station].set_index("time")
component_chart = selected_likely[["upstream_stage_change_m", "local_stage_change_m", "change_m"]].rename(columns={
    "upstream_stage_change_m": "Routed upstream contribution",
    "local_stage_change_m": "Local rainfall/runoff contribution",
    "change_m": "Total stage change",
})
st.markdown("**Likely stage-change decomposition (+ rise / − fall)**")
st.line_chart(component_chart, height=280)

flow_chart = selected_likely[["routed_upstream_cms", "local_runoff_cms", "total_station_cms"]].rename(columns={
    "routed_upstream_cms": "Routed upstream flow",
    "local_runoff_cms": "Local runoff",
    "total_station_cms": "Total station flow index",
})
st.markdown("**Routed flow + local runoff at selected station**")
st.line_chart(flow_chart, height=280)

st.subheader("6) Calibration / Interpretation")
st.markdown(
    """
**How to improve this after each real rain event:**
1. Update the station Current EL values at forecast start.
2. Save the forecast CSV below.
3. Later compare predicted vs actual PAGASA hourly levels.
4. Adjust each reach's **wave speed, Muskingum X, effective river-length factor, incremental catchment area, local runoff response time, and stage m/+100 m³/s**.
5. Replace approximate station coordinates/reach lengths with verified geometry, then replace the temporary Wawa spill equation once an official/derived spill rating curve is available.

The most valuable calibration target is **change in river elevation (ΔH)** rather than absolute EL, because ΔH can be compared consistently from event to event.
"""
)

# Export combined results
export_wawa = base_sim.copy()
export_wawa["record_type"] = "wawa_simulation"
export_stage = base_stage.copy()
export_stage["record_type"] = "downstream_stage"

csv1 = export_wawa.to_csv(index=False).encode("utf-8")
csv2 = export_stage.to_csv(index=False).encode("utf-8")
csv3 = operational.to_csv(index=False).encode("utf-8")

d1, d2, d3 = st.columns(3)
with d1:
    st.download_button("Download Wawa 120h CSV", csv1, "wawa_120h_simulation.csv", "text/csv", use_container_width=True)
with d2:
    st.download_button("Download station hydrographs", csv2, "marikina_station_forecast.csv", "text/csv", use_container_width=True)
with d3:
    st.download_button("Download forecast summary", csv3, "marikina_forecast_summary.csv", "text/csv", use_container_width=True)

st.info(
    "Model status: EXPERIMENTAL / CALIBRATION MODE. The authoritative river-level reference is the official PAGASA "
    "Pasig-Marikina-Tullahan FFWS Water Level Map. No third-party river-level fallback is used. If the official page "
    "cannot be read automatically, the forecast stops unless an official table paste or manual Current EL is supplied. "
    "Open-Meteo rainfall is forecast guidance, not an official PAGASA rainfall forecast. PAGASA warnings and bulletins override this simulation."
)
