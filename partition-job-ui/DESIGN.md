# Design system — PartOps

## World
**PartOps** — GTN EDB Partition Operations Platform. Commercial PostgreSQL ops console with Light / Dark / System themes via CSS variables (`--pj-*`). Light: cream paper (`#f3f0e9` / `#fbfaf7`). Dark: deep charcoal/navy content surfaces. Charcoal rail (`#202c34`) in both modes. Lime identity (`#e5ff5c`), primary blue (`#165dff` / brighter in dark). Restrained status greens/ambers/reds. No glassmorphism, no purple gradients, no neon glow.

## Typography
- UI sans: `Geist` (or `Inter` fallback only if Geist unavailable via next/font).
- Mono: `Geist Mono` / `ui-monospace` for cron, SQL, errors, IDs.
- Display headings: tight tracking (−0.03em), heavy weight; section labels uppercase 0.12em tracking, muted.

## Shell
- Fixed left sidebar ~248px, permanent navigation (Overview, Convert, Create, Jobs, History, System readiness).
- Compact PartOps mark + wordmark; no Workspace card.
- Theme toggle (Light / Dark / System) in sidebar Appearance section.
- No duplicate top nav tabs.
- Page header: eyebrow “PartOps”, title, subtitle, page actions only (Export, New job).
- Compact SYSTEM footer in sidebar: scheduler + database dots → links to `/system`.

## Components
- KPI cards: subtle 1px border, soft offset shadow, large tabular numeral.
- Tables: dense rows, status dots, CREATE/DROP badges (blue / amber).
- Drawers for job detail and error inspection.
- Primary / secondary / danger button system.

## Motion
- Sidebar active indicator and page content fade/slide 180–240ms ease-out.
- Health banner and KPI entrance once on Overview.

## Authority
Reference: https://aesthetix-ashen.vercel.app — interpret, do not clone copy or fake numbers.
