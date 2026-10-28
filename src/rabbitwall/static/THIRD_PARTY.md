# Third-party assets

## Natural Earth

`world.json` is a reduced, rounded derivative of Natural Earth's 1:110m Admin 0 Countries data. Natural Earth places its map data in the public domain, including commercial use and redistribution. RabbitWall's MIT license does not change that status.

- [Original data](https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_110m_admin_0_countries.geojson)
- [Official terms](https://www.naturalearthdata.com/about/terms-of-use/)
- Source URL and original SHA-256 are recorded in `world.json`.
- Rebuild: `python3 tools/build_world_map.py downloaded-countries.geojson`

Representative points describe countries, not the position of an individual IP.

## DB-IP Country Lite (optional, downloaded separately)

IP Geolocation by [DB-IP](https://db-ip.com), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). RabbitWall extracts IPv4 rows from the original CSV and converts them to SQLite. The database records its edition, source, license URL and this modification notice.

The source archive does not contain this database. The console retains the provider link on every page and links to the license. Preserve both when using the results in another UI. If redistributing a generated database, retain its metadata and this attribution; do not label the data MIT. These terms concern **Lite**, not a paid DB-IP database. See the [provider's terms](https://db-ip.com/db/lite.php).

## Application assets

JavaScript, CSS and visualization code are maintained in this repository. No external JavaScript library, CDN, web font, NICTER code, logo or imagery is bundled. The RabbitWall hero image is project-generated artwork.

## dnstap and external DNS components

The bundled official dnstap schema and generated Python binding use the schema's CC0 dedication. See [schema provenance and rebuild instructions](../../../third_party/dnstap/README.md). Unbound (BSD license) and Python Protocol Buffers (BSD 3-Clause) are installed separately, not bundled in the source archive. dnspython remains an external ISC-licensed dependency for parsing observation messages, not DNS forwarding.
