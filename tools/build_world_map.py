#!/usr/bin/env python3
"""Reduce the public-domain Natural Earth 110m country GeoJSON for the console."""
import argparse
import hashlib
import json
from pathlib import Path
import re

SOURCE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"


def rounded(value):
    if isinstance(value, list):
        return [rounded(item) for item in value]
    return round(value, 2) if isinstance(value, (int, float)) else value


def convert(raw):
    source = json.loads(raw)
    features, points = [], {}
    for feature in source["features"]:
        properties, geometry = feature["properties"], feature["geometry"]
        code = properties.get("ISO_A2_EH") or properties.get("ISO_A2")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code):
            code = None
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        features.append({"code": code, "polygons": rounded(polygons)})
        if code and isinstance(properties.get("LABEL_X"), (int, float)) and isinstance(properties.get("LABEL_Y"), (int, float)):
            points[code] = {"point": rounded([properties["LABEL_X"], properties["LABEL_Y"]]),
                            "name": properties.get("NAME_JA") or properties.get("NAME_EN") or code}
    return {"source": "Natural Earth 1:110m Admin 0 Countries", "license": "public domain",
            "source_url": SOURCE, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "features": features, "countries": points}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "src/rabbitwall/static/world.json")
    args = parser.parse_args()
    data = convert(args.input.read_bytes())
    args.output.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{len(data['features'])} shapes / {len(data['countries'])} country reference points: {args.output}")


if __name__ == "__main__":
    main()
