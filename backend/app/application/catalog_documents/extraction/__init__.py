"""DCIM01 PDF datasheet import, PR-B: safe extraction of candidate specification values.

Everything that touches a PDF's bytes runs in a sandboxed child process (see `sandbox`).
Extraction produces *candidates* with provenance; it never writes to a catalog revision."""
