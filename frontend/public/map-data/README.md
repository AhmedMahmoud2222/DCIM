# Optional basemap override directory

This directory is the optional **override** location for the dashboard's offline country basemap. Vite copies
it unchanged into the image, and nginx serves it same-origin at `/map-data/`.

It ships **without data files** on purpose: with no `manifest.json` here, the browser gets a 404 for
`/map-data/manifest.json` and silently uses the basemap bundled into the frontend build
(`frontend/src/features/dashboard/basemap/`).

To ship a different basemap, install a validated dataset here before building the image (or bind-mount a
directory at `/usr/share/nginx/html/map-data`, read-only) with the offline tool:

```sh
python scripts/dcim_basemap.py install <validated-dir> --target frontend/public/map-data
python scripts/dcim_basemap.py status --target frontend/public/map-data
```

This README is harmless: it is only ever served as a static file at `/map-data/README.md`, and the browser
never requests it. It contains no site data.

The full procedure is in the [offline map data guide](../../../docs/OFFLINE_MAP_DATA.md).
