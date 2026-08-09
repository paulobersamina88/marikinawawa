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
BUILD_ID = "2026-08-09-PAGASA-PASTE-FALLBACK-v4"

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

# Open-Meteo forecast points. These are point forecasts, not official PAGASA gauges.
RAIN_POINTS = {
    "San Jose, Antipolo": (14.61944, 121.28194),
    "Tanay, Rizal": (14.49850, 121.28560),
    "Marikina": (14.65070, 121.10290),
}

# Map coordinates for operational visualization. Upper Wawa is based on the
# mapped Upper Wawa Dam location. Downstream gauge coordinates are initial
# approximate plotting points and should be replaced with verified station
# coordinates when available; they do not affect the hydraulic calculations.
WAWA_DAM_COORD = (14.70072, 121.20310)
STATION_MAP_COORDS = {
    "Montalban": (14.7315, 121.1510),
    "Rodriguez": (14.7160, 121.1260),
    "Nangka": (14.6830, 121.1085),
    "Sto Nino": (14.6507, 121.1029),
    "Tumana Bridge": (14.6715, 121.0965),
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
        # IMPORTANT: Current EL is intentionally blank. The app must obtain it
        # from the official PAGASA Water Level Map, an exact table pasted from
        # that page, or a deliberate manual override. Old sample EL values are
        # never silently reused. Warning thresholds are static starting values
        # and are overwritten whenever the official table supplies them.
        {"station": "Montalban", "current_el_m": None, "alert_el_m": 22.40, "alarm_el_m": 23.00, "critical_el_m": 23.60,
         "lag_hr": 1, "attenuation": 0.96, "stage_m_per_100cms": 0.11, "local_rain_m_per_10mm": 0.05},
        {"station": "Rodriguez", "current_el_m": None, "alert_el_m": 28.80, "alarm_el_m": 29.80, "critical_el_m": 30.70,
         "lag_hr": 1, "attenuation": 0.94, "stage_m_per_100cms": 0.12, "local_rain_m_per_10mm": 0.06},
        {"station": "Nangka", "current_el_m": None, "alert_el_m": 16.50, "alarm_el_m": 17.10, "critical_el_m": 17.70,
         "lag_hr": 2, "attenuation": 0.90, "stage_m_per_100cms": 0.14, "local_rain_m_per_10mm": 0.08},
        {"station": "Sto Nino", "current_el_m": None, "alert_el_m": 15.00, "alarm_el_m": 16.00, "critical_el_m": 17.00,
         "lag_hr": 3, "attenuation": 0.86, "stage_m_per_100cms": 0.16, "local_rain_m_per_10mm": 0.10},
        {"station": "Tumana Bridge", "current_el_m": None, "alert_el_m": 17.26, "alarm_el_m": 18.26, "critical_el_m": 19.26,
         "lag_hr": 4, "attenuation": 0.82, "stage_m_per_100cms": 0.18, "local_rain_m_per_10mm": 0.12},
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
    series = {}
    for name, (lat, lon) in RAIN_POINTS.items():
        df = fetch_open_meteo_hourly(lat, lon)
        series[name] = df.set_index("time")["rain_mm"].rename(name)
    merged = pd.concat(series.values(), axis=1).sort_index().fillna(0.0)
    merged["wawa_proxy_mm"] = (
        merged["San Jose, Antipolo"] * 0.65
        + merged["Tanay, Rizal"] * 0.35
    )
    # Local downstream rainfall forcing: primarily Marikina, with Antipolo contribution.
    merged["local_proxy_mm"] = (
        merged["Marikina"] * 0.70
        + merged["San Jose, Antipolo"] * 0.30
    )
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


def local_rain_memory(rain_series: pd.Series, decay: float = 0.82) -> pd.Series:
    vals = []
    memory = 0.0
    for rain in rain_series.fillna(0.0):
        memory = memory * decay + float(rain)
        vals.append(memory)
    return pd.Series(vals, index=rain_series.index)


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


def build_downstream_stage_forecast(sim_df: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    out = []
    if sim_df.empty:
        return pd.DataFrame()

    baseline_q = float(sim_df["wawa_downstream_cms"].iloc[0])
    local_memory = local_rain_memory(sim_df["local_proxy_mm"])

    for _, s in stations.iterrows():
        lag = max(int(s["lag_hr"]), 0)
        attenuation = float(s["attenuation"])
        flow_sensitivity = float(s["stage_m_per_100cms"])
        rain_sensitivity = float(s["local_rain_m_per_10mm"])

        routed_q = sim_df["wawa_downstream_cms"].shift(lag, fill_value=baseline_q) * attenuation
        routed_base = baseline_q * attenuation
        excess_q = (routed_q - routed_base).clip(lower=0.0)
        wawa_rise = (excess_q / 100.0) * flow_sensitivity

        # Local rainfall is expressed as an exponentially decaying wetness/runoff index.
        local_rise = (local_memory / 10.0) * rain_sensitivity

        stage = float(s["current_el_m"]) + wawa_rise + local_rise
        temp = pd.DataFrame({
            "time": sim_df["time"].values,
            "station": s["station"],
            "predicted_el_m": stage,
            "rise_m": wawa_rise + local_rise,
            "wawa_rise_m": wawa_rise,
            "local_rain_rise_m": local_rise,
            "routed_wawa_cms": routed_q,
            "alert_el_m": s["alert_el_m"],
            "alarm_el_m": s["alarm_el_m"],
            "critical_el_m": s["critical_el_m"],
        })
        temp["status"] = temp.apply(
            lambda r: stage_status(r["predicted_el_m"], r["alert_el_m"], r["alarm_el_m"], r["critical_el_m"]),
            axis=1,
        )
        out.append(temp)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def scenario_summary(stage_df: pd.DataFrame, stations: pd.DataFrame, scenario_name: str) -> pd.DataFrame:
    rows = []
    for _, s in stations.iterrows():
        part = stage_df[stage_df["station"] == s["station"]].copy()
        if part.empty:
            continue
        peak_idx = part["predicted_el_m"].idxmax()
        peak = part.loc[peak_idx]
        rows.append({
            "scenario": scenario_name,
            "station": s["station"],
            "current_el_m": float(s["current_el_m"]),
            "peak_el_m": round(float(peak["predicted_el_m"]), 2),
            "predicted_rise_m": round(float(peak["rise_m"]), 2),
            "peak_time": pd.Timestamp(peak["time"]).strftime("%b %d, %I:%M %p"),
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
        rise = max(float(row.get("likely_rise_m", 0.0) or 0.0), 0.0)
        radius = 8 + min(rise * 9.0, 22.0)

        popup = f"""
        <div style='font-size:13px;min-width:280px'>
          <b>{name}</b><br>
          Likely status: <b>{status}</b><br>
          Current EL: <b>{float(row['current_el_m']):.2f} m</b><br>
          Expected rise: <b>+{rise:.2f} m</b><br>
          Low peak: {float(row['low_peak_m']):.2f} m<br>
          Likely peak: <b>{float(row['likely_peak_m']):.2f} m</b><br>
          High peak: {float(row['high_peak_m']):.2f} m<br>
          Likely peak time: <b>{row['likely_peak_time']} PHT</b><br><br>
          Alert: {float(sm['alert_el_m']):.2f} m<br>
          Alarm: {float(sm['alarm_el_m']):.2f} m<br>
          Critical: {float(sm['critical_el_m']):.2f} m<br>
          <small>Marker size represents predicted rise; color represents likely peak status.</small>
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
                f"likely {float(row['likely_peak_m']):.2f} m (+{rise:.2f} m)"
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
      <small>Station marker size = predicted rise</small>
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
    "Experimental screening model: forecast rainfall → 262 km² Wawa catchment runoff → assumed spillway routing → downstream Marikina stage-rise estimate."
)

st.warning(
    "This is NOT an official flood forecast. The spill rating curve, surcharge storage, travel times, attenuation, and flow-to-stage sensitivities are temporary calibration assumptions. "
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
    "Calibration coefficients below remain experimental. Current EL and warning thresholds are seeded from the selected official PAGASA source above."
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
        "lag_hr": st.column_config.NumberColumn("Wawa lag (hr)", min_value=0, max_value=12, step=1),
        "attenuation": st.column_config.NumberColumn("Flow attenuation", min_value=0.2, max_value=1.2, step=0.01, format="%.2f"),
        "stage_m_per_100cms": st.column_config.NumberColumn("Stage m / +100 m³/s", min_value=0.01, max_value=1.0, step=0.01, format="%.2f"),
        "local_rain_m_per_10mm": st.column_config.NumberColumn("Stage m / 10 mm local index", min_value=0.0, max_value=1.0, step=0.01, format="%.2f"),
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

# Pre-seed the future rolling wetness by adding historical rainfall into a helper prefix.
def with_antecedent(future_df: pd.DataFrame, past_df: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([past_df, future_df], ignore_index=True).sort_values("time")
    combined["antecedent_seed"] = combined["wawa_proxy_mm"].rolling(48, min_periods=1).sum()
    out = combined[combined["time"] >= future_df["time"].min()].copy().reset_index(drop=True)
    # The reservoir router recomputes rolling rainfall. Preserve the seeded value by
    # back-calculating with a synthetic prehistory effect through a small correction column.
    # Simpler: attach and use this column below after routing for display only.
    return out

forecast_seeded = with_antecedent(forecast, past48)

# For runoff coefficient calculation with real antecedent rain, create a prefixed dataframe,
# run the model, then trim to future 120 h. This avoids assuming hour 1 starts dry.
def run_scenario(name, rain_factor, c_offset):
    prefix = past48.copy()
    future = forecast.copy()
    combo = pd.concat([prefix, future], ignore_index=True).sort_values("time").reset_index(drop=True)
    sim_combo = route_reservoir(
        combo,
        initial_level_m=current_wawa_el,
        abstraction_mld=abstraction_mld,
        env_flow_cms=env_flow_cms,
        catchment_km2=catchment_km2,
        surface_area_km2=surface_area_km2,
        fsl_m=fsl_m,
        spill_k=spill_k,
        spill_exp=spill_exp,
        base_inflow_cms=base_inflow_cms,
        rain_multiplier=rain_factor,
        c_offset=c_offset,
    )
    # IMPORTANT: reservoir state should start at 'now', not 48 h ago. Re-run future only
    # but seed C from actual antecedent rainfall via a temporary adjusted first-48h series.
    # To preserve correct initial reservoir level, build future C manually from past+future.
    all_rain = pd.concat([past48[["time", "wawa_proxy_mm"]], future[["time", "wawa_proxy_mm"]]], ignore_index=True)
    all_rain["scaled"] = all_rain["wawa_proxy_mm"] * rain_factor
    all_rain["ant48"] = all_rain["scaled"].rolling(48, min_periods=1).sum()
    ant_future = all_rain.tail(len(future))["ant48"].reset_index(drop=True)

    custom = future.copy().reset_index(drop=True)
    custom["wawa_rain_mm"] = custom["wawa_proxy_mm"] * rain_factor
    custom["antecedent_48h_mm"] = ant_future
    custom["runoff_c"] = custom["antecedent_48h_mm"].apply(lambda x: runoff_coefficient(x, c_offset))
    custom["raw_runoff_cms"] = 0.278 * custom["runoff_c"] * custom["wawa_rain_mm"] * catchment_km2

    # Lag runoff with a short prehistory so the first forecast hours inherit recent runoff.
    raw_hist = 0.278 * past48["wawa_proxy_mm"].reset_index(drop=True).apply(
        lambda mm: runoff_coefficient(antecedent_wawa_mm, c_offset) * (mm * rain_factor) * catchment_km2
    ) if not past48.empty else pd.Series(dtype=float)
    raw_all = pd.concat([raw_hist.tail(3), custom["raw_runoff_cms"]], ignore_index=True)
    lagged_all = lag_runoff(raw_all, [0.20, 0.45, 0.25, 0.10])
    custom["rain_inflow_override"] = lagged_all.tail(len(custom)).reset_index(drop=True)

    # Custom reservoir routing using the precomputed inflow/C values.
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
    custom["scenario"] = name

    stage = build_downstream_stage_forecast(custom, stations)
    stage["scenario"] = name
    return custom, stage


base_sim, base_stage = run_scenario("Likely", 1.00, 0.00)
low_sim, low_stage = run_scenario("Low rain", low_rain_factor, -0.05)
high_sim, high_stage = run_scenario("High rain", high_rain_factor, high_c_offset)

# -----------------------------------------------------------------------------
# DASHBOARD
# -----------------------------------------------------------------------------
st.subheader("2) 5-Day Rainfall + Wawa Basin Forcing")
forecast_total_wawa = float(forecast["wawa_proxy_mm"].sum())
forecast_total_local = float(forecast["local_proxy_mm"].sum())
peak_idx = forecast["wawa_proxy_mm"].idxmax()
peak_rain = forecast.loc[peak_idx]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Past 48h Wawa-proxy rain", f"{antecedent_wawa_mm:.1f} mm")
m2.metric("Next 120h Wawa-proxy rain", f"{forecast_total_wawa:.1f} mm")
m3.metric("Next 120h local rain index", f"{forecast_total_local:.1f} mm")
m4.metric("Peak Wawa-proxy hour", f"{float(peak_rain['wawa_proxy_mm']):.1f} mm/h")
st.caption(f"Peak forecast basin-proxy rainfall: {pd.Timestamp(peak_rain['time']).strftime('%b %d, %I:%M %p')} PHT")

rain_chart = forecast.set_index("time")[["San Jose, Antipolo", "Tanay, Rizal", "Marikina", "wawa_proxy_mm"]]
st.line_chart(rain_chart, height=300)

with st.expander("Show 120-hour rainfall table"):
    rain_table = forecast[["time", "San Jose, Antipolo", "Tanay, Rizal", "Marikina", "wawa_proxy_mm", "local_proxy_mm"]].copy()
    rain_table["time"] = rain_table["time"].dt.strftime("%Y-%m-%d %H:%M")
    st.dataframe(rain_table, use_container_width=True, hide_index=True)

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
    "Predicted elevations = current observed EL + estimated Wawa-spill rise + local-rainfall rise. "
    "The stage sensitivities are temporary until calibrated against actual PAGASA hydrographs."
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
        "likely_peak_time": likely_sum.loc[station, "peak_time"],
        "likely_status": likely_sum.loc[station, "peak_status"],
    })
operational = pd.DataFrame(rows)
st.dataframe(operational, use_container_width=True, hide_index=True)

st.subheader("5) Upper Wawa → Marikina Forecast Map")
st.caption(
    "Interactive monitoring map: river-station color = likely peak status; marker size = predicted rise. "
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
    "Map note: Upper Wawa uses a mapped dam coordinate. Downstream station points are initial approximate plotting locations for visualization only; "
    "they do not affect the forecast calculation and should be replaced with verified gauge coordinates when available."
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

# Decompose likely rise into Wawa and local rainfall components.
selected_likely = base_stage[base_stage["station"] == selected_station].set_index("time")
component_chart = selected_likely[["wawa_rise_m", "local_rain_rise_m", "rise_m"]]
st.markdown("**Likely predicted rise decomposition**")
st.line_chart(component_chart, height=280)

st.subheader("6) Calibration / Interpretation")
st.markdown(
    """
**How to improve this after each real rain event:**
1. Update the station Current EL values at forecast start.
2. Save the forecast CSV below.
3. Later compare predicted vs actual PAGASA hourly levels.
4. Adjust each station's **lag**, **attenuation**, **stage m/+100 m³/s**, and **local-rain sensitivity**.
5. Replace the temporary Wawa spill equation once an official/derived spill rating curve is available.

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
