#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31",  # HTTP client for the Immich API
#     "pyyaml>=6.0",     # YAML frontmatter for the .md summary files
# ]
# ///
# Run directly: ./immich-export.py --url ... --api-key ...
# (the uv shebang + the PEP 723 block above make the script self-contained:
#  uv creates/reuses an ephemeral venv and installs dependencies on first run)

"""Export assets from Immich: original file + .jpg.md description + .xmp GPS sidecar.

Usage:
  ./immich-export.py --url https://immich.example.com --api-key KEY [--album-id ID]
                     [--album-regex REGEX] [--asset-id ID] [--out DIR] [--limit N]

Auth: API key (Settings > API Keys in Immich). No login flow needed.
Endpoints used (v1/v2 stable):
  GET /api/assets/{id}          -> exifInfo.description / latitude / longitude
  GET /api/assets/{id}/original -> the original, unmodified file
"""

import argparse
import re
import sys
from pathlib import Path

import requests
import yaml


def api(base: str, key: str, path: str) -> dict:
    r = requests.get(f"{base.rstrip('/')}{path}",
                     headers={"x-api-key": key, "Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    return r.json()


def api_post(base: str, key: str, path: str, body: dict) -> dict:
    r = requests.post(f"{base.rstrip('/')}{path}", json=body,
                      headers={"x-api-key": key, "Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    return r.json()


def search_assets(base: str, key: str, body: dict) -> list:
    """Page through POST /search/metadata (the v3 way to list assets)."""
    assets, page = [], 1
    while True:
        r = api_post(base, key, "/api/search/metadata", {**body, "page": page, "size": 100})
        batch = (r.get("assets") or {}).get("items") or []
        if not batch:
            break
        assets.extend(batch)
        page += 1
    return assets


def download_original(base: str, key: str, asset_id: str, dest: Path) -> None:
    with requests.get(f"{base.rstrip('/')}/api/assets/{asset_id}/original",
                      headers={"x-api-key": key}, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def to_dms(value: float, ref: str) -> str:
    """Convert decimal degrees to XMP DMS string, e.g. '52,31,6.1236N'."""
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    return f"{d},{m},{s:.4f}{ref}"


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def face_regions(faces: list, img_w: int, img_h: int) -> str:
    """Build MWG-RS + IPTC ImageRegion face-region XML from Immich face data.

    faces: entries from GET /api/faces (with person.name and boundingBoxX1..Y2,
    in pixels of imageWidth x imageHeight — normalized to 0..1 here).
    """
    if not faces:
        return ""
    mwg_items, iptc_items = [], []
    for i, f in enumerate(faces):
        name = (f.get("person") or {}).get("name") or ""
        if not name:
            continue  # skip unnamed/unrecognized faces
        fw, fh = f.get("imageWidth") or img_w, f.get("imageHeight") or img_h
        if not fw or not fh:
            continue
        x1 = f["boundingBoxX1"] / fw
        x2 = f["boundingBoxX2"] / fw
        y1 = f["boundingBoxY1"] / fh
        y2 = f["boundingBoxY2"] / fh
        # MWG-RS (Lightroom/digiKam lineage)
        mwg_items.append(
            '     <mwg-rs:Area x="{x1:.6f}" y="{y1:.6f}" w="{w:.6f}" h="{h:.6f}"'
            ' unit="normalized"/>\n'
            '     <mwg-rs:Name>{name}</mwg-rs:Name>'.format(
                x1=x1, y1=y1, w=x2 - x1, h=y2 - y1, name=xml_escape(name))
        )
        # IPTC ImageRegion (current official standard)
        iptc_items.append(
            "     <Iptc4xmpExt:RegionBoundary x=\"{x1:.6f}\" y=\"{y1:.6f}\" w=\"{w:.6f}\" h=\"{h:.6f}\"/>"
            .format(x1=x1, y1=y1, w=x2 - x1, h=y2 - y1) +
            (f"\n     <Iptc4xmpExt:PersonName>{xml_escape(name)}</Iptc4xmpExt:PersonName>"
             if name else "")
        )
    if not mwg_items:
        return ""
    return (
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"\n'
        '    xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">\n'
        '   <mwg-rs:Regions>\n'
        '    <mwg-rs:RegionList>\n'
        + "\n".join(
            f"     <rdf:Description>\n{m}\n     </rdf:Description>" for m in mwg_items
        ) +
        "\n    </mwg-rs:RegionList>\n   </mwg-rs:Regions>\n"
        "   <Iptc4xmpExt:ImageRegion>\n"
        + "\n".join(f"    <rdf:Description>\n{i}\n    </rdf:Description>" for i in iptc_items) +
        "\n   </Iptc4xmpExt:ImageRegion>\n"
        "  </rdf:Description>\n"
    )


def write_xmp(path: Path, lat: float | None, lon: float | None, description: str = "",
              date_taken: str = "", faces_xml: str = "") -> None:
    if lat is not None and lon is not None:
        lat_ref = "N" if lat >= 0 else "S"
        lon_ref = "E" if lon >= 0 else "W"
        gps_attrs = (f'    xmlns:exif="http://ns.adobe.com/exif/1.0/"\n'
                     f'    exif:GPSLatitude="{to_dms(lat, lat_ref)}"\n'
                     f'    exif:GPSLongitude="{to_dms(lon, lon_ref)}"\n'
                     f'    exif:GPSLatitudeRef="{lat_ref}"\n'
                     f'    exif:GPSLongitudeRef="{lon_ref}">\n')
    else:
        gps_attrs = ">\n"
    desc = xml_escape(description)
    xmp = f"""<?xml version='1.0' encoding='utf-8'?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
{gps_attrs}   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{desc}</rdf:li></rdf:Alt></dc:description>
   <xmp:CreateDate>{date_taken}</xmp:CreateDate>
  </rdf:Description>
{faces_xml} </rdf:RDF>
</x:xmpmeta>
"""
    path.write_text(xmp, encoding="utf-8")


def yaml_quote(s: str) -> str:
    """Serialize a scalar to a safe single-line YAML value (no document markers)."""
    return yaml.safe_dump(s, default_flow_style=True, allow_unicode=True,
                          width=10**6, explicit_start=False, explicit_end=False
                          ).strip().removesuffix("...").strip()


def write_md(path: Path, a: dict) -> None:
    """Markdown summary with YAML frontmatter: machine-parseable fields + human-readable body."""
    exif = a.get("exifInfo") or {}
    place = ", ".join(x for x in (exif.get("city"), exif.get("state"), exif.get("country")) if x)
    lat, lon = exif.get("latitude"), exif.get("longitude")
    people = [p.get("name") for p in (a.get("people") or []) if p.get("name")]

    # YAML frontmatter (Obsidian-style: --- delimited block at the top)
    fm = ["---",
          f"file: {yaml_quote(a.get('originalFileName', a['id']))}",
          f"asset_id: {a['id']}",
          f"type: {a.get('type', 'IMAGE')}"]
    if exif.get("description"):
        fm.append(f"description: {yaml_quote(exif['description'])}")
    if exif.get("dateTimeOriginal"):
        fm.append(f"date: {exif['dateTimeOriginal']}")
    if lat is not None and lon is not None:
        fm.append(f"location: {{lat: {lat}, lon: {lon}}}")
        if exif.get("city"):
            fm.append(f"city: {yaml_quote(exif['city'])}")
        if exif.get("state"):
            fm.append(f"state: {yaml_quote(exif['state'])}")
        if exif.get("country"):
            fm.append(f"country: {yaml_quote(exif['country'])}")
    if exif.get("make") or exif.get("model"):
        fm.append(f"camera: {yaml_quote(str(exif.get('make') or '').strip() + ' ' + str(exif.get('model') or '')).strip('\" ')}")
    if people:
        fm.append(f"people: [{', '.join(yaml_quote(p) for p in people)}]")
    fm.append("---")

    # human-readable markdown body
    lines = fm + [
        "",
        f"# {a.get('originalFileName', a['id'])}",
        "",
        f"- Description: {exif.get('description') or '—'}",
        f"- Date: {exif.get('dateTimeOriginal') or '—'}",
    ]
    if lat is not None and lon is not None:
        lines.append(f"- Location: {lat:.6f}, {lon:.6f}" + (f" ({place})" if place else ""))
        lines.append(f"- Map: https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}")
    else:
        lines.append("- Location: —")
    if exif.get("model"):
        lines.append(f"- Camera: {str(exif.get('make') or '').strip()} {exif['model']}".strip())
    if people:
        lines.append(f"- People: {', '.join(people)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_asset(base: str, key: str, asset_id: str, out: Path,
                 want_image: bool = True, want_xmp: bool = True, want_md: bool = True) -> bool:
    """Export one asset (original + .xmp + .md) honoring the selection flags."""
    full = api(base, key, f"/api/assets/{asset_id}")
    name = full["originalFileName"]
    if want_image:
        download_original(base, key, asset_id, out / name)
    exif = full.get("exifInfo") or {}
    lat, lon = exif.get("latitude"), exif.get("longitude")
    faces_xml = ""
    if want_xmp:
        try:
            faces = api(base, key, f"/api/faces?id={asset_id}")
            faces_xml = face_regions(faces, exif.get("exifImageWidth") or 0,
                                     exif.get("exifImageHeight") or 0)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                print("  ! no face.read permission on API key — skipping XMP face regions "
                      "(edit the key's permissions in Immich to include it)")
            else:
                raise
        write_xmp(out / f"{name}.xmp", lat, lon,
                  exif.get("description") or "",
                  exif.get("dateTimeOriginal") or "", faces_xml)
    if want_md:
        write_md(out / f"{name}.md", full)
    return True


def sanitize(name: str) -> str:
    """Make an album name safe as a directory name."""
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or "unnamed"


def export_album(base: str, key: str, album_id: str, out: Path, limit: int = 0,
                 want_image: bool = True, want_xmp: bool = True, want_md: bool = True) -> int:
    """Export one album's assets (images + videos) into `out`; returns count."""
    out.mkdir(parents=True, exist_ok=True)
    assets = search_assets(base, key, {"albumIds": [album_id]})
    if limit:
        assets = assets[:limit]
    n = 0
    for a in assets:
        export_asset(base, key, a["id"], out, want_image, want_xmp, want_md)
        n += 1
        print(f"[{n}] {a.get('originalFileName', a['id'])}")
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="e.g. https://immich.example.com")
    p.add_argument("--api-key", required=True)
    p.add_argument("--asset-id", help="export a single asset by ID")
    p.add_argument("--album-id", help="export all assets of an album")
    p.add_argument("--album-regex", help="export every album whose name matches this regex")
    p.add_argument("--limit", type=int, default=0, help="max assets (0 = all)")
    p.add_argument("--out", default="immich-export")
    p.add_argument("--no-image-download", action="store_true",
                   help="skip downloading the original files (metadata/sidecars only)")
    p.add_argument("--no-xmp-sidecar", action="store_true",
                   help="skip writing .xmp sidecars (GPS/description/faces)")
    p.add_argument("--no-summary-file", action="store_true",
                   help="skip writing .md summary files")
    args = p.parse_args()
    if args.no_image_download and args.no_xmp_sidecar and args.no_summary_file:
        p.error("nothing to export: all three outputs disabled")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.album_regex:
        pattern = re.compile(args.album_regex)
        albums = api(args.url, args.api_key, "/api/albums")
        matches = [al for al in albums if pattern.search(al.get("albumName", ""))]
        if not matches:
            print(f"No album matches /{args.album_regex}/", file=sys.stderr)
            return 1
        for al in matches:
            print(f"Album: {al['albumName']} ({al.get('assetCount', '?')} assets)")
            export_album(args.url, args.api_key, al["id"], out / sanitize(al["albumName"]),
                         args.limit, not args.no_image_download, not args.no_xmp_sidecar,
                         not args.no_summary_file)
        return 0

    if args.asset_id:
        assets = [{"id": args.asset_id}]
    elif args.album_id:
        n = export_album(args.url, args.api_key, args.album_id, out, args.limit,
                         not args.no_image_download, not args.no_xmp_sidecar,
                         not args.no_summary_file)
        print(f"Done: {n} assets -> {out.resolve()}")
        return 0
    else:
        assets = search_assets(args.url, args.api_key, {})
        if args.limit:
            assets = assets[: args.limit]

    n = 0
    for a in assets:
        export_asset(args.url, args.api_key, a["id"], out,
                     not args.no_image_download, not args.no_xmp_sidecar,
                     not args.no_summary_file)
        n += 1
        print(f"[{n}] {a.get('originalFileName', a['id'])}")

    print(f"Done: {n} assets -> {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
