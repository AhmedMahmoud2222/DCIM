# Commercial UI uplift plan

## Product direction

The interface becomes a dark, restrained operations workspace rather than an engineering console. Slate-charcoal surfaces keep dense data legible; indigo identifies navigation and primary actions; emerald, amber, orange, and rose are reserved for actual status semantics. The main visual anchor is an operational dashboard with an exception rail, not oversized inventory totals.

## Reused architecture

The uplift retains the React Router application shell, TanStack Query data lifecycle, `apiFetch` authentication path, Tailwind stack, existing dashboard, topology, spatial, rack-elevation, telemetry, alarm, integration, and collector APIs. No operational values are created in the frontend.

## Route and component work

* App shell: grouped sidebar, identity, contextual breadcrumbs, usable narrow-screen behavior.
* Shared primitives: page header, metric card, status badge, section title, and explicit empty state.
* Dashboard: operational health and exception-first layout, compact inventory.
* Infrastructure, racks, equipment, floor plan, power, collectors, integrations, and managed assets: shared panels, status language, intentional loading/empty/error states.
* Rack elevation: replace raw document navigation with React Router navigation, preserving the in-memory access-token session.

## Design constraints

The product never implies device, collector, telemetry, alarm, or capacity health when the API reports no data. Where APIs lack relationships (for example a room-to-building breadcrumb endpoint) the UI keeps a concise known context instead of guessing.
