# immich-export

Export your photos out of [Immich](https://immich.app) with their metadata intact:
the original unmodified file, an `.xmp` sidecar (GPS, description, date, face
regions), and a readable `.md` summary for each asset.

> **Note:** This is an AI vibe coded project — written and iterated on with an AI
> assistant, not hand-crafted line by line. It works, but treat it accordingly:
> test with `--limit 1` before pointing it at your whole library.

## What you get per photo

| File | Contents |
|---|---|
| `photo.jpg` | The original, unmodified file |
| `photo.jpg.xmp` | GPS coordinates (EXIF), description (dc:description), date taken, face regions (MWG-RS + IPTC ImageRegion — understood by Lightroom, digiKam, etc.) |
| `photo.jpg.md` | Human-readable summary: description, date, location with an OpenStreetMap link, camera, people |

## Requirements

- [uv](https://docs.astral.sh/uv/) — recommended; the script is self-contained
  (`uv` reads its inline dependency manifest and sets up an ephemeral venv
  automatically on first run)
- Or plain Python >= 3.10 with `requests` installed

You'll also need an **Immich API key**: Immich web UI → *Settings → API Keys*.
Required key permissions:

| Permission | Needed for |
|---|---|
| Asset → Read | Listing assets and fetching their metadata |
| Asset → Download | Downloading the original files |
| Face → Read | Face regions in the XMP files (optional — skipped with a warning) |
| Album → Read | Only for `--album-regex`, which lists your albums to match names (`--album-id` doesn't need it) |

## Usage

```sh
# Make it executable once
chmod +x immich-export.py

# Export everything (images only)
./immich-export.py --url https://immich.example.com --api-key KEY

# Export a single album
./immich-export.py --url https://immich.example.com --api-key KEY --album-id ID

# Export every album whose name matches a regex (each into its own subdirectory)
./immich-export.py --url https://immich.example.com --api-key KEY --album-regex ".*2024.*"

# Export a single asset
./immich-export.py --url https://immich.example.com --api-key KEY --asset-id ID

# Dry-run-ish: just a few files to see what the output looks like
./immich-export.py --url https://immich.example.com --api-key KEY --limit 3
```

### Options

| Option | Default | Description |
|---|---|---|
| `--url` | (required) | Immich server URL |
| `--api-key` | (required) | API key from Immich settings |
| `--asset-id` | | Export a single asset by ID |
| `--album-id` | | Export all assets of one album |
| `--album-regex` | | Export every album whose name matches this regex |
| `--limit` | `0` | Max assets (`0` = all; per album in `--album-regex` mode) |
| `--out` | `immich-export/` | Output directory |
