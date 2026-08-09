# Marikina + Upper Wawa Spatial Rainfall & River Routing v6

Build: `2026-08-09-SPATIAL-RAIN-MUSKINGUM-v6`

This version combines the two major hydrologic ideas requested for the Marikina monitoring app:

1. **Spatially varying hourly rainfall** from Upper Wawa down to Marikina.
2. **Sequential river routing** from Upper Wawa → Montalban → Rodriguez → Nangka → Sto Nino → Tumana Bridge.

## Model architecture

- Upper Wawa uses the declared **262 km² catchment** for the rainfall-runoff calculation.
- Wawa reservoir level and spill are routed with the app's temporary level-pool / assumed ungated-spill equation.
- Each downstream reach has its **own hourly rainfall time series** rather than one common local-rain index.
- Each reach generates **incremental local runoff** using an editable effective local catchment area and runoff-response time.
- The complete upstream hydrograph is routed with an hourly **Muskingum K-X** reach model.
- Local runoff is added to routed upstream flow, and the resulting discharge is passed to the next downstream reach.
- Because each rainfall field and reach has its own timing, the stage forecast can rise, fall, show delayed peaks, or show multiple peaks when hydrographs overlap.

## Temporary routing geometry

The app estimates straight-line reach distances from the current map points and multiplies them by an editable **river-length/meander factor**. Muskingum K is then estimated from effective reach length / editable flood-wave speed. This makes length and travel time explicit, but it is still calibration geometry—not surveyed channel geometry.

The app exposes these parameters for calibration:

- flood-wave speed (km/h)
- Muskingum X
- incremental local area (km²)
- local runoff response time (hr)
- stage response time (hr)
- stage sensitivity (m per +100 m³/s)
- global river-length factor

## Rainfall forcing

Open-Meteo point forecasts are used as screening rainfall inputs for:

- Upper Wawa
- San Jose, Antipolo
- Tanay, Rizal
- Montalban / Rodriguez
- Nangka
- Marikina

The app derives separate rainfall forcing time series for each river reach and displays their peak timing and 120-hour totals.

## River levels

Official PAGASA Pasig-Marikina-Tullahan FFWS Water Level Map remains the authoritative Current EL reference. The app tries automatic extraction first. If that fails, paste the official table directly into the provided field. It never silently reuses stale sample Current EL values.

## Important limitation

This remains an **experimental calibration / screening model**, not an official flood forecast. Approximate station coordinates, effective reach lengths, incremental areas, wave speeds, Muskingum X values, spill curve, and stage sensitivities must be calibrated/replaced using verified geometry and observed hydrographs before operational interpretation. An already-travelling flood wave from changes before forecast initialization may not be fully represented when only the PAGASA map Current EL (without historical discharge/stage series) is available.
