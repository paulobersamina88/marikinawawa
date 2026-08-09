# Marikina + Upper Wawa Forecast — PAGASA Paste Fallback v4

Authoritative river-level source:
https://pasig-marikina-tullahanffws.pagasa.dost.gov.ph/water/map.do

## River-level input behavior
1. The app first attempts to read the official PAGASA Water Level Map automatically.
2. If the website cannot be read by the Streamlit server, this is shown as a warning rather than a model error.
3. Paste the entire PAGASA water-level table into the blank **Paste PAGASA Water Level table here** field.
4. The app automatically extracts only Montalban, Rodriguez, Nangka, Sto Nino and Tumana Bridge.
5. A parsed preview is shown before the values are used.
6. Old/sample Current EL values are never silently substituted.
7. If both automatic and pasted data are available, automatic data remain the default; a checkbox allows the pasted browser copy to override them when it is newer.

Paste format can include tabs, repeated spaces, `(*)` flags, and leading/trailing asterisks. It is safe to paste all PAGASA stations; unrelated stations are ignored.

The hydraulic model remains experimental/calibration-mode and should not replace PAGASA or local emergency warnings.
