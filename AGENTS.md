# AGENTS.md

## Overview

Single-file Python CLI tool: `immich-export.py` exports assets from an Immich server
(original file + `.xmp` sidecar with GPS/description/faces + `.md` summary). There is
no package, no test suite, no CI, and no git history — everything lives in the one script.

## Running

```sh
# Preferred: uv runs it self-contained (shebang + PEP 723 inline metadata declare
# the sole dependency, requests>=2.31; uv builds an ephemeral venv on first run)
./immich-export.py --url https://immich.example.com --api-key KEY [--album-id ID]
                   [--album-regex REGEX] [--asset-id ID] [--out DIR] [--limit N]

# Equivalent manual smoke test (needs Python >=3.10 and requests installed):
python immich-export.py --url ... --api-key ... --limit 1
```

- `--limit 0` (default) means "all". In `--album-regex` mode the limit applies
  **per album**, not globally.
- `--album-regex` uses `re.search` on album names; no match exits with code 1.
- Default output dir is `immich-export/`; album exports go into
  `out/<sanitized-album-name>/` (unsafe filename chars replaced with `_`).
- There is no build/lint/test tooling configured. The only practical check is a
  syntax pass (`python -m py_compile immich-export.py`) plus a manual run with
  `--limit 1` or `--asset-id` against a live server.

## Architecture (data flow)

1. Select assets: `POST /api/search/metadata` (paged, `page` starts at 1,
   `size=100`, stops on first empty batch) — filtered by album or type `IMAGE`;
   or a single `--asset-id`.
2. Per asset: `GET /api/assets/{id}` to fetch the full record.
3. `GET /api/assets/{id}/original` streamed to disk in 1 MiB chunks (300 s timeout;
   JSON calls use 60 s).
4. `GET /api/faces?id={assetId}` for face regions (best-effort, see gotchas).
5. Write sidecars: `write_xmp()` (GPS in DMS, dc:description, xmp:CreateDate,
   face regions in **both** MWG-RS and IPTC ImageRegion forms) and `write_md()`
   (description, date, location + OpenStreetMap link, camera, people).

## Gotchas

- **Search listings are slim.** `/api/search/metadata` items lack `exifInfo` and
  may lack a reliable `type`, so every asset is re-fetched via
  `GET /api/assets/{id}`. Never trust the listing payload beyond `id`.
- **Auth is the `x-api-key` header**, not Bearer. Key permissions (verified
  against Immich's route guards): `AssetRead` (search + asset detail) and
  `AssetDownload` (`/original`) are always required; `FaceRead` is optional
  (403 handled); `AlbumRead` is needed only by `--album-regex` (`GET /api/albums`)
  — `--album-id` filters through search/metadata, which is guarded by
  `AssetRead` alone.
- **`GET /api/faces` returns 403** when the API key lacks `face.read` — this is
  caught and downgraded to a warning; any other HTTP error is re-raised. Keep that
  distinction if touching the error handling.
- **Only `type == "IMAGE"` assets are exported**; videos are silently skipped
  after the full fetch (so they still cost one API call each).
- **The per-asset export loop exists twice** — in `export_album()`
  (immich-export.py:189) and inline in `main()` (immich-export.py:262) for the
  `--asset-id` / no-filter paths. Any behavioral change must be applied to both
  (or unify them).
- **XMP is hand-built from f-strings** with a manual `xml_escape()`; no XML
  library is used anywhere. All user text (descriptions, person names) must go
  through `xml_escape()` before insertion.
- **Face bounding boxes arrive in pixels** of each face record's
  `imageWidth`/`imageHeight` (falling back to the asset's
  `exifImageWidth`/`exifImageHeight`) and are normalized to 0..1 in
  `face_regions()`.
- GPS is written as comma-separated DMS (e.g. `52,31,6.1236N`) with explicit
  lat/lon ref attributes; latitude and longitude are treated as a pair (skipped
  unless both are present).

## Conventions

- Stdlib + `requests` only; dependency is declared exclusively in the PEP 723
  block at the top of the script — keep that block in sync if deps change.
- Target Python >= 3.10 (uses `float | None` union syntax).
- Functions take `(base, key, ...)` for API accessors; `base` is `rstrip('/')`-ed
  at each call site, so callers may pass a URL with or without trailing slash.
- Docstrings carry the "why"; module docstring doubles as user-facing usage docs.
