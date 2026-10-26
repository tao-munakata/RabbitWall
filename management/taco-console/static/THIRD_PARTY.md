# Map data

`world.json` is a reduced, rounded derivative of Natural Earth's 1:110m Admin 0 Countries data.
The map source is in the public domain. Source URL and SHA-256 are recorded in `world.json`.

- Source: https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_110m_admin_0_countries.geojson
- Terms: https://www.naturalearthdata.com/about/terms-of-use/
- Rebuild: `python3 management/build_world_map.py downloaded-countries.geojson`

Country representative points are for country-level display, not IP-level coordinates.
Geolocation labels use the separately installed DB-IP Lite data; its attribution appears in the console footer.

The visualization code is implemented in this project. No NICTER source, logo, or imagery is included.
