# Physical-value storage inventory

| Area | Values | Current storage contract | Boundary policy |
|---|---|---|---|
| Telemetry mappings/readings | temperature, humidity, power, load, availability | source unit on mapping; canonical unit/value plus raw value/unit/version on new readings | canonical registry conversion during ingestion |
| Telemetry latest-status cache | watts, volts, amps, Celsius, percentages in JSON payloads | explicit field-name units | validate at the status API; migration to registry-backed structured series is separate work |
| Catalog revisions | dimensions, weight, rack units | value paired with explicit `dimension_unit`/`weight_unit`; published revisions immutable | conversion only when publishing into legacy fixed mm/kg tables |
| Catalog power templates | watts, volts | unit encoded in field name or documented volts | validate API fields; no implicit conversion |
| Datasheet candidates | parsed value/unit and exact raw value/unit/source text | provenance-preserving candidate rows with registry version | extraction never overwrites evidence; apply workflow performs target conversion |
| Power topology/capacity | kW, volts | unit encoded in schema field name | service boundary accepts documented unit only |
| Spatial geometry | mm and pixels depending on model/layer contract | unit encoded in field name or layer metadata | keep geometry transformations explicit |

Rack units (`U`) and counts are not physical-unit conversions. Historical columns whose unit is encoded in the column name remain backward compatible; this decision does not silently reinterpret or rewrite them.
