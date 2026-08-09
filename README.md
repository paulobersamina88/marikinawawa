# Marikina River + Upper Wawa 120-Hour Forecast

Experimental Streamlit screening model for Marikina monitoring.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Model chain

Open-Meteo hourly rainfall -> proxy Upper Wawa basin rainfall -> rainfall/runoff over 262 km² -> assumed Upper Wawa spill routing -> lag/attenuation to downstream stations -> experimental river-stage rise.

## Important assumptions

- Upper Wawa catchment: 262 km² (working declared catchment used by the user)
- Reservoir surface area: 4.5 km² approximation based on ~450 ha published reservoir area
- Spill crest/FSL default: 135.00 m
- Temporary spill curve: Q = 430 (H - 135)^1.5 for H > 135 m
- Wawa proxy rainfall: 65% San Jose, Antipolo + 35% Tanay, Rizal
- Local downstream rainfall index: 70% Marikina + 30% San Jose, Antipolo
- Downstream travel times, attenuation, and flow-to-stage sensitivities are calibration assumptions editable in the app
- Forecast uncertainty uses low/likely/high rainfall scenarios, not statistical probabilities

## Operational use

Before interpreting the forecast, update the Current EL and warning levels with the latest PAGASA observations. Save forecasts and compare with actual station hydrographs after each event to calibrate lag and stage-response coefficients.

This is not an official PAGASA flood forecast and should not be used as the sole basis for public safety decisions.

## Interactive forecast map

The app includes a FloodWatch-style Folium map with:
- Upper Wawa Dam current/forecast spill condition
- Montalban, Rodriguez, Nangka, Sto Nino, and Tumana Bridge forecast nodes
- marker color based on likely peak status (Normal/Alert/Alarm/Critical)
- marker size based on predicted river-stage rise
- low/likely/high predicted peak EL in station popups
- San Jose Antipolo, Tanay Rizal, and Marikina 120-hour rainfall nodes
- toggleable layers and a schematic Wawa-to-downstream monitoring path

Upper Wawa uses a mapped dam coordinate. Downstream station coordinates are initial approximate plotting locations for visualization only and do not affect the hydraulic calculations; replace them with verified gauge coordinates when available.
