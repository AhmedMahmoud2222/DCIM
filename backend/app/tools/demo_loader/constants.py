"""Names, markers and defaults shared by the demo loader. Changing a marker changes what `purge` can recognise."""

from app.tools.demo_loader import LOADER_VERSION

DEMO_SITE_PREFIX = "DEMO-SITE-"
POLL_INTERVAL_FALLBACK_SECONDS = 300

# Every synthetic reading carries these attributes. `provenance` is the key `purge` and `verify` filter on.
PROVENANCE = "demo_loader"
GENERATOR = "dcim-demo-loader"
PROFILE = "diurnal-v1"


def reading_attributes() -> dict:
    """Constant per loader version, so a re-run produces byte-identical rows for the same slot."""
    return {
        "synthetic": True,
        "provenance": PROVENANCE,
        "generator": GENERATOR,
        "generator_version": LOADER_VERSION,
        "profile": PROFILE,
        "note": "Simulated value for a local demonstration. Not a measurement.",
    }


DEDUP_PREFIX = "demo-sim:v1:"
SOURCE_PREFIX = "demo-sim:"  # source_identifier prefix of every mapping the loader creates
MAPPING_LABEL = "Demo simulator"
SENSOR_TAG_PREFIX = "DEMO-ENV-"
SENSOR_MARKER_KEY = "demo_loader"

DEVICE_METRICS = ("power_kw", "load_percent")
SENSOR_METRICS = ("temperature_c", "humidity_percent")

DEFAULT_HISTORY_HOURS = 24
# The requirement is 24 hours for every series, devices included. Devices are 1,000 series, so a full run is long
# (see docs/DEMO_LOCAL_LOADER.md for the measured throughput); `--device-history-hours` lets a pilot use less.
DEFAULT_DEVICE_HISTORY_HOURS = 24
MAX_BATCH = 500
