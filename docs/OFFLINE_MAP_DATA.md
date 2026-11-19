# Offline map data (dashboard basemap)

**Date:** 2026-11-18 · **Scope:** Issue #365, PR #367 · **Tool:** `scripts/dcim_basemap.py` (Python 3.11+, stdlib only)

The Control Room dashboard draws country boundaries from a locally packaged, version-pinned basemap. The browser
never requests map tiles, CDNs, web fonts or geocoders: every map byte comes from the frontend's own origin. This
guide covers the file formats, how a new dataset is built and validated offline, how it is installed, verified and
rolled back, and what the browser does when the data is missing or bad.

## 1. Purpose and data separation

| | Basemap (this guide) | DCIM site data |
| --- | --- | --- |
| Content | Country polygons, names, ISO 3166-1 alpha-2 codes | Sites, cities, countries, coordinates |
| Classification | Public reference data (Natural Earth, public domain) | Confidential operational data |
| Delivered by | Static files from the nginx frontend container | Authorized API (`GET /api/v1/dashboard/geography`) |
| Changed by | Rebuilding/redeploying the frontend image or its mounted directory | Normal DCIM editing, scope-checked |

The two are never mixed: a basemap file contains no DCIM data, and the backend never serves basemap files. The UI
joins them only in the browser by matching a site country's ISO code to the basemap `iso2`. Precise site
coordinates are an API authorization matter, described in the [Control Room API contract](DASHBOARD_CONTROL_ROOM_API.md).

## 2. File formats

Both files are UTF-8 JSON. The supported `schema_version` set is exactly `{1}`; any other value is "incompatible".

**Data file** (`format: "dcim-basemap"`):

```json
{"format":"dcim-basemap","schema_version":1,"dataset_id":"natural-earth-admin0-50m","version":"5.1.2-r1",
 "projection":"equirectangular","precision":100,
 "countries":[{"iso2":"AE","name":"United Arab Emirates","polygons":[[[5170,2420,5180,2430,5160,2440]]]}]}
```

- Every coordinate is an **integer**, degrees x 100 (longitude -18000..18000, latitude -9000..9000).
- A polygon is a list of rings: ring 0 is the outer boundary, the rest are holes. A ring is a flat array
  `[lon0, lat0, lon1, lat1, ...]` with an even length, at least 3 points, and is **not** closed by repeating the
  first point. Winding order is not significant.
- `iso2` matches `^[A-Z]{2}$` and is unique, or is `null` (drawn, never matched to DCIM countries). `name` is a
  non-empty string of at most 128 characters.
- Limits: at most 400 countries, 250,000 points in total and 4 MiB per file.

**Manifest** (`format: "dcim-basemap-manifest"`):

```json
{"format":"dcim-basemap-manifest","schema_version":1,"dataset_id":"natural-earth-admin0-50m","version":"5.1.2-r1",
 "file":"basemap-5.1.2-r1.json","bytes":454070,"sha256":"<64 lowercase hex>","created_at":"2026-11-18T00:00:00Z",
 "source":{"name":"Natural Earth","url":"https://github.com/nvkelso/natural-earth-vector/tree/v5.1.2",
           "license":"Public domain","source_sha256":"<sha256 of the source GeoJSON>"}}
```

- `file` matches `^[A-Za-z0-9._-]{1,128}\.json$` and never contains `..`: a manifest cannot point outside its own
  directory or to another origin.
- `bytes` and `sha256` must match the data file exactly; `dataset_id` and `version` must equal the data file's.

## 3. Bundled basemap and optional override

- **Bundled (always present):** `frontend/src/features/dashboard/basemap/world-50m.v1.json` with
  `world-50m.v1.manifest.json`, built from Natural Earth 1:50m admin-0 countries, tag `v5.1.2`. It is part of the
  immutable frontend build, loaded only on the dashboard, and validated in CI (`backend/tests/unit/test_basemap_tool.py`).
- **Override (optional):** same-origin static files at `/map-data/manifest.json` and `/map-data/<manifest.file>`,
  served by the frontend nginx container (`location /map-data/` returns a real 404 when a file is absent, with
  `Cache-Control: no-store`). The repository directory `frontend/public/map-data/` ships with only a
  [README](../frontend/public/map-data/README.md), so the bundled map is used by default.

There is **no upload endpoint** by design: a basemap changes only through the deployment process, with the same
review and access control as any other frontend change.

## 4. Build a new dataset offline

Download the source once, on a machine with internet access, and carry it to the build host. Keep the source
GeoJSON out of the repository.

```sh
# On a connected machine (one-off; about 3 MB, public domain):
curl -fLo ne_50m_admin_0_countries.geojson \
  https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_50m_admin_0_countries.geojson
sha256sum ne_50m_admin_0_countries.geojson   # v5.1.2: 3e458fc036ad0a66411f2c1e6cac49c5d7bfb81cb1123bc513b22511a2b7fdeb

# On the (offline) build host, from the repository root:
python scripts/dcim_basemap.py build --geojson ne_50m_admin_0_countries.geojson \
  --out-dir /tmp/basemap-5.1.2-r2 --version 5.1.2-r2
```

`build` rounds to 0.01 degree, removes duplicate points, simplifies each ring (Douglas-Peucker, `--tolerance`, default
0.03 degree, never below 4 points), drops islands smaller than `--min-area` (default 0.0005 square degrees) unless that
would remove the country's last polygon, takes `iso2` from `ISO_A2_EH` falling back to `ISO_A2`, and records the source
SHA-256 in the manifest. The output is byte-stable: the same input and options always give the same SHA-256.
Natural Earth already splits antimeridian-crossing countries (Russia, Fiji); the rings are kept as given. Other options:
`--file`, `--manifest-name`, `--dataset-id`, `--source-url`, `--source-license`, `--created-at`.

The bundled files were produced with:

```sh
python scripts/dcim_basemap.py build --geojson ne_50m_admin_0_countries.geojson \
  --out-dir frontend/src/features/dashboard/basemap --version 5.1.2-r1 --file world-50m.v1.json \
  --manifest-name world-50m.v1.manifest.json --created-at 2026-11-18T00:00:00Z
```

## 5. Validate, install, verify and roll back

```sh
python scripts/dcim_basemap.py validate /tmp/basemap-5.1.2-r2                  # checks every rule in section 2
python scripts/dcim_basemap.py install /tmp/basemap-5.1.2-r2 --target frontend/public/map-data
python scripts/dcim_basemap.py status --target frontend/public/map-data       # active and previous version
python scripts/dcim_basemap.py rollback --target frontend/public/map-data     # restore the previous manifest
```

- Exit code 0 only on success; every failure prints `ERROR: <reason>` and exits 1.
- `install` validates the source directory first. It then copies the data file (temp file + `os.replace`), keeps the
  current `manifest.json` as `manifest.previous.json`, and atomically replaces `manifest.json`. It never deletes or
  overwrites an older data file (a different file with the same name is refused: use a new version), refuses a
  `--target` containing `..`, and on any validation failure leaves the disk unchanged.
- `rollback` validates `manifest.previous.json` and its data file, saves the current manifest as
  `manifest.rolledback.json`, then atomically moves the previous manifest into place. With no previous manifest it
  fails without changing anything.
- `status` exits 1 when an active manifest exists but does not validate; "none" means the bundled basemap is in use.
- To return to the bundled map, remove `manifest.json` from the override directory (or the mount) and redeploy.

## 6. Ship through the deployment process

Choose one:

1. **Bake into the image:** run `install` with `--target frontend/public/map-data`, commit through a reviewed PR, and
   build the frontend image as usual (Vite copies `public/` into the image).
2. **Read-only bind mount** of an operator-managed directory, prepared with `install`. No container restart is
   needed after an update, because the files are served with `no-store`. Example for a compose override file
   (not committed by this change):

```yaml
services:
  frontend:
    volumes:
      - /srv/dcim/map-data:/usr/share/nginx/html/map-data:ro
```

Run `status` against the directory after every change. A mount replaces the image's `map-data` directory entirely.

## 7. What the browser does

1. It fetches `/map-data/manifest.json` (same origin, `no-store`, no credentials). A 404, network error or non-JSON
   body means "no override" and is silent: the bundled map is used.
2. Otherwise it validates the manifest, fetches the data file, and checks its size, SHA-256 and content.
3. **Any** failure after step 1 (invalid or incompatible manifest, size or checksum mismatch, corrupt or invalid data,
   data fetch error) falls back to the bundled map and shows a short note such as "Custom basemap 5.2.0 was rejected
   (checksum mismatch); using bundled basemap 5.1.2-r1."
4. The panel shows a one-line provenance caption (source, dataset and version). A slow or failed basemap never blocks
   the dashboard; the accessible country/city/site list remains the authoritative equivalent of the map.

## 8. Integrity is not authenticity

The SHA-256 in the manifest detects corruption, truncation and mixed or partial deployments. It does **not** prove
who produced the file: anyone able to replace both files through the deployment process can also replace the hash.
Authenticity comes from deployment access control and code review. Signed manifests are a possible future option and
are out of scope for this change.

## 9. License and attribution

Natural Earth data is in the public domain; no permission is required. Keep the `source` block (name, URL, license,
source SHA-256) in every manifest so the provenance and attribution ("Made with Natural Earth") stay with the data.

## 10. Update cadence and review checklist

Natural Earth releases rarely; update when a release fixes a boundary or name that matters to your sites, and at
most as part of a normal release. For each update, the reviewer checks:

- [ ] Source downloaded from the pinned Natural Earth tag; its SHA-256 matches `source.source_sha256`.
- [ ] New `version` string (never reuse a published version or file name).
- [ ] `validate` passes; country count and size are plausible (the bundled build has 242 countries, about 450 KB).
- [ ] Countries hosting DCIM sites still have their `iso2` (spot-check `status` and the dashboard highlight).
- [ ] No DCIM data and no external URL in the files other than the manifest's `source.url`.
- [ ] After deployment: `status` reports the new version as active and valid; the dashboard caption shows it.

## 11. Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Caption says "bundled" although an override was deployed | `manifest.json` missing, or `/map-data/` not served by this nginx config | `status --target <dir>`; check the mount path and the `location /map-data/` block |
| Note "checksum mismatch" or "size mismatch" | Data file edited, truncated or from another build | Re-run `install` from a validated directory, or `rollback` |
| Note "incompatible schema" | File built for a newer schema | Rebuild with this repository's `scripts/dcim_basemap.py` |
| `install` refuses "already exists with different content" | Reused file name for different data | Build with a new `--version` |
| `rollback` says "no previous manifest" | Nothing installed before, or already rolled back | Install the wanted version explicitly |
| A small country is not highlighted | `iso2` is `null` for that entity in the source | Check the source's `ISO_A2_EH`/`ISO_A2`; document the exception |
| `build` fails "above the 4194304 limit" | Tolerance too low | Raise `--tolerance` or `--min-area` |
