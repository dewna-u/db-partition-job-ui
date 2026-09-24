# Design system — Partition Manager

## World
Commercial PostgreSQL operations console. Cream paper workspace (`#f3f0e9` / `#fbfaf7`), charcoal rail (`#202c34`), lime identity mark (`#e5ff5c`), primary action blue (`#165dff`). Restrained status greens/ambers/reds. No glassmorphism, no purple gradients, no neon glow.

## Typography
- UI sans: `Geist` (or `Inter` fallback only if Geist unavailable via next/font).
- Mono: `Geist Mono` / `ui-monospace` for cron, SQL, errors, IDs.
- Display headings: tight tracking (−0.03em), heavy weight; section labels uppercase 0.12em tracking, muted.

## Shell
- Fixed left sidebar ~248px, permanent navigation (Overview, Convert, Create, Jobs, History).
- No duplicate top nav tabs.
- Page header: eyebrow “Operations console”, title, subtitle, page actions only (Export, New job).
- Compact SYSTEM footer in sidebar: scheduler + database dots, last sync.

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
