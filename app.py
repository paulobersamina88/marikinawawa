import math
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
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
        # Current values below are placeholders carried from the user's working dataset.
        # Replace them in the app with the latest PAGASA values before interpreting results.
        {"station": "Montalban", "current_el_m": 24.98, "alert_el_m": 22.40, "alarm_el_m": 23.00, "critical_el_m": 23.60,
         "lag_hr": 1, "attenuation": 0.96, "stage_m_per_100cms": 0.11, "local_rain_m_per_10mm": 0.05},
        {"station": "Rodriguez", "current_el_m": 29.81, "alert_el_m": 28.80, "alarm_el_m": 29.80, "critical_el_m": 30.70,
         "lag_hr": 1, "attenuation": 0.94, "stage_m_per_100cms": 0.12, "local_rain_m_per_10mm": 0.06},
        {"station": "Nangka", "current_el_m": 22.21, "alert_el_m": 16.50, "alarm_el_m": 17.10, "critical_el_m": 17.70,
         "lag_hr": 2, "attenuation": 0.90, "stage_m_per_100cms": 0.14, "local_rain_m_per_10mm": 0.08},
        {"station": "Sto Nino", "current_el_m": 15.51, "alert_el_m": 15.00, "alarm_el_m": 16.00, "critical_el_m": 17.00,
         "lag_hr": 3, "attenuation": 0.86, "stage_m_per_100cms": 0.16, "local_rain_m_per_10mm": 0.10},
        {"station": "Tumana Bridge", "current_el_m": 11.97, "alert_el_m": 17.26, "alarm_el_m": 18.26, "critical_el_m": 19.26,
         "lag_hr": 4, "attenuation": 0.82, "stage_m_per_100cms": 0.18, "local_rain_m_per_10mm": 0.12},
    ]
)

PAGASA_WATER_URL = "https://pasig-marikina-tullahanffws.pagasa.dost.gov.ph/water/map.do"
PAGASA_RAIN_URL = "https://pasig-marikina-tullahanffws.pagasa.dost.gov.ph/rainfall/map.do"


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

    refresh = st.button("Refresh rainfall now", use_container_width=True)
    if refresh:
        st.cache_data.clear()
        st.rerun()

    st.link_button("Open PAGASA Water Level", PAGASA_WATER_URL, use_container_width=True)
    st.link_button("Open PAGASA Rainfall", PAGASA_RAIN_URL, use_container_width=True)


# Editable station table
st.subheader("1) Current PAGASA River Levels + Temporary Calibration")
st.caption(
    "Update Current EL from the latest PAGASA table. The lag, attenuation and stage-sensitivity columns are experimental and can be calibrated after real events."
)
stations = st.data_editor(
    DEFAULT_STATIONS,
    use_container_width=True,
    hide_index=True,
    num_rows="fixed",
    column_config={
        "station": st.column_config.TextColumn("Station", disabled=True),
        "current_el_m": st.column_config.NumberColumn("Current EL (m)", format="%.2f"),
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
    "Model status: EXPERIMENTAL / CALIBRATION MODE. Open-Meteo rainfall is forecast guidance, not an official PAGASA rainfall forecast. "
    "PAGASA warning levels and official bulletins should override this simulation."
)
