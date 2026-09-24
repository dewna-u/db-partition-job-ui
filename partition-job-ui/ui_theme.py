"""Visual theme CSS for the partition.ops Streamlit console."""

from __future__ import annotations


def theme_css() -> str:
    return """
<style>
:root {
  --pj-bg: #f3f0e9;
  --pj-card: #fbfaf7;
  --pj-line: #ddd9d0;
  --pj-border: #d9d5cc;
  --pj-ink: #1c2730;
  --pj-muted: #718078;
  --pj-faint: #8a958e;
  --pj-primary: #165dff;
  --pj-primary-soft: #e7edff;
  --pj-green: #1f8a64;
  --pj-green-bg: #e4f3ea;
  --pj-amber: #c76b2d;
  --pj-amber-bg: #fff0e5;
  --pj-red: #c4473a;
  --pj-red-bg: #fff0ee;
  --pj-lime: #e5ff5c;
  --pj-sidebar: #202c34;
  --pj-sidebar-2: #293840;
  --pj-radius: 13px;
}
html, body, .stApp {
  background:
    radial-gradient(circle at 74% 0%, #dbe8ff 0, transparent 27rem),
    linear-gradient(135deg, #f3f0e9 0%, #eeece5 100%) !important;
  color: var(--pj-ink);
  font-family: Arial, Helvetica, sans-serif;
}
.stApp > header, [data-testid="stHeader"] { background: transparent !important; }
[data-testid="stToolbar"] { right: 1rem !important; }

/* Always-visible control to reopen the sidebar after collapse */
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"],
button[kind="headerNoPadding"] {
  display: flex !important;
  visibility: visible !important;
  opacity: 1 !important;
  z-index: 999999 !important;
  position: fixed !important;
  top: 0.85rem !important;
  left: 0.85rem !important;
  width: 42px !important;
  height: 42px !important;
  border-radius: 10px !important;
  border: 1px solid #cac6bc !important;
  background: #faf9f5 !important;
  color: #1c2730 !important;
  box-shadow: 0 8px 20px #202c3420 !important;
}
[data-testid="collapsedControl"] svg,
[data-testid="stSidebarCollapsedControl"] svg {
  color: #1c2730 !important;
}

.block-container {
  max-width: 1500px !important;
  padding-top: 1.35rem !important;
  padding-bottom: 2rem !important;
  padding-left: 2.4rem !important;
  padding-right: 2.4rem !important;
}

/* Sidebar */
section[data-testid="stSidebar"] {
  background: var(--pj-sidebar) !important;
  border-right: 1px solid #cfcac0 !important;
  min-width: 246px !important;
}
section[data-testid="stSidebar"] > div:first-child {
  background: var(--pj-sidebar) !important;
  padding: 1.4rem 0.9rem 1rem !important;
}
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {
  color: #aab5b1 !important;
}
section[data-testid="stSidebar"] [data-testid="stRadio"] > label { display:none !important; }
section[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] {
  display:flex !important; flex-direction:column !important; gap:0.25rem !important;
}
section[data-testid="stSidebar"] [data-testid="stRadio"] label {
  color:#b0bbb8 !important; font-size:0.75rem !important; font-weight:600 !important;
  padding:0.7rem 0.65rem !important; border-radius:9px !important;
  border:1px solid transparent !important; background:transparent !important; margin:0 !important;
}
section[data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
  color:#fff !important; background:#2e3d45 !important;
}
section[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) {
  color:#202c34 !important; background:var(--pj-lime) !important; border-color:var(--pj-lime) !important;
}
section[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) p,
section[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) span {
  color:#202c34 !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] {
  background:#293840 !important; border:1px solid #526067 !important; border-radius:11px !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary,
section[data-testid="stSidebar"] [data-testid="stExpander"] p,
section[data-testid="stSidebar"] [data-testid="stExpander"] span {
  color:#c5ceca !important;
}
section[data-testid="stSidebar"] .stButton > button {
  background:#293840 !important; color:#f3f1e9 !important; border:1px solid #526067 !important;
  border-radius:9px !important; font-size:0.72rem !important; font-weight:700 !important;
}
section[data-testid="stSidebar"] .stAlert {
  background:#293840 !important; border:1px solid #526067 !important; color:#d5ddd9 !important;
}

/* Top nav (always available when sidebar is closed) */
.pj-topnav {
  display:flex; flex-wrap:wrap; gap:0.4rem; align-items:center;
  margin: 0 0 1rem 0; padding: 0.55rem 0.65rem;
  border:1px solid var(--pj-line); border-radius:12px; background:#fbfaf7cc;
}
.pj-topnav-label {
  font-size:0.62rem; letter-spacing:.12em; text-transform:uppercase;
  color:#86908a; font-weight:800; margin-right:0.35rem;
}

h1 {
  font-size:2.05rem !important; letter-spacing:-0.06em !important; line-height:1.05 !important;
  font-weight:800 !important; color:var(--pj-ink) !important; margin-bottom:0.15rem !important;
}
div[data-testid="stCaptionContainer"] { color:var(--pj-muted) !important; font-size:0.82rem !important; }

label[data-testid="stWidgetLabel"] p { font-size:0.72rem !important; font-weight:700 !important; color:#5f6d68 !important; }
.stTextInput input, .stNumberInput input, .stTextArea textarea,
.stSelectbox [data-baseweb="select"] > div, .stDateInput input, .stTimeInput input {
  background:#fff !important; border:1px solid #d1cdc4 !important; border-radius:8px !important;
  color:var(--pj-ink) !important; font-size:0.78rem !important; min-height:34px !important;
}
.stTextArea textarea { font-family:ui-monospace,SFMono-Regular,Menlo,monospace !important; }

.stButton > button {
  border-radius:9px !important; border:1px solid #c9c6bd !important; background:#fbfaf7 !important;
  color:#3f4e4a !important; font-size:0.72rem !important; font-weight:700 !important;
  padding:0.62rem 0.95rem !important; min-height:34px !important; box-shadow:none !important;
}
.stButton > button:hover { border-color:#7f8d87 !important; background:#fff !important; color:var(--pj-ink) !important; }
.stButton > button:disabled { opacity:0.45 !important; background:#efece4 !important; color:#8a958e !important; }
.stButton > button[kind="primary"], .stButton > button[data-testid="baseButton-primary"] {
  background:var(--pj-primary) !important; border-color:var(--pj-primary) !important; color:#fff !important;
  box-shadow:0 8px 20px #165dff2b !important;
}
.stButton > button[kind="primary"]:hover { background:#0f4fd6 !important; border-color:#0f4fd6 !important; color:#fff !important; }
div[data-testid="element-container"]:has(.pj-btn-run) + div[data-testid="element-container"] button {
  background:#1f8a64 !important; border-color:#1f8a64 !important; color:#fff !important;
  box-shadow:0 8px 20px #1f8a642b !important;
}
div[data-testid="element-container"]:has(.pj-btn-danger) + div[data-testid="element-container"] button {
  background:#c76b2d !important; border-color:#c76b2d !important; color:#fff !important;
}
.pj-btn-marker { display:none !important; }

[data-testid="stMetric"] {
  background:var(--pj-card); border:1px solid var(--pj-line); border-radius:12px;
  padding:1.05rem 1.1rem 0.95rem; box-shadow:0 4px 15px #4c554c08;
}
[data-testid="stMetricLabel"] { color:#7e8982 !important; font-size:0.68rem !important; font-weight:700 !important; }
[data-testid="stMetricValue"] { font-size:1.65rem !important; letter-spacing:-0.06em !important; font-weight:800 !important; }

[data-testid="stDataFrame"], [data-testid="stDataFrameResizable"] {
  border:1px solid var(--pj-line) !important; border-radius:12px !important; overflow:hidden !important;
  background:var(--pj-card) !important; box-shadow:0 7px 22px #4c554c09;
}
[data-testid="stVerticalBlockBorderWrapper"] {
  border:1px solid var(--pj-line) !important; border-radius:13px !important;
  background:var(--pj-card) !important; box-shadow:0 7px 22px #4c554c09 !important;
}

.pj-brand { display:flex; align-items:center; gap:0.7rem; padding:0 0.35rem 1.1rem;
  border-bottom:1px solid #45535a; margin-bottom:0.85rem; }
.pj-brand-icon { display:grid; place-items:center; width:34px; height:34px; border-radius:10px;
  background:var(--pj-lime); color:#202c34; font-weight:900; transform:rotate(-6deg); }
.pj-brand strong { display:block; font-size:1rem; letter-spacing:-0.045em; color:#f3f1e9 !important; }
.pj-brand strong span { color:var(--pj-lime); }
.pj-brand small { display:block; margin-top:0.2rem; color:#aab5b1 !important; font-size:0.62rem; }
.pj-workspace { display:flex; align-items:center; gap:0.55rem; margin:0.2rem 0 1rem; padding:0.65rem 0.7rem;
  border:1px solid #526067; border-radius:11px; background:#293840; }
.pj-workspace-avatar { display:grid; place-items:center; width:28px; height:28px; border-radius:8px;
  background:var(--pj-lime); color:#202c34; font-weight:800; font-size:0.58rem; }
.pj-workspace span { display:block; font-size:0.62rem; color:#a5b0ad !important; }
.pj-workspace strong { display:block; margin-top:0.1rem; font-size:0.75rem; color:#fff !important; }
.pj-nav-kicker { padding:0 0.4rem; margin:0.15rem 0 0.45rem; font-size:0.62rem; letter-spacing:0.13em;
  text-transform:uppercase; color:#8e9b98 !important; font-weight:700; }
.pj-nav-kicker.second { margin-top:1.2rem; }
.pj-connection { display:flex; align-items:center; gap:0.55rem; padding:0.85rem 0.35rem; margin-top:0.5rem;
  border-top:1px solid #45535a; border-bottom:1px solid #45535a; }
.pj-live-dot { width:6px; height:6px; border-radius:50%; background:#7de0ad; box-shadow:0 0 0 4px #7de0ad22; }
.pj-live-dot.off { background:#c76b2d; box-shadow:0 0 0 4px #c76b2d22; }
.pj-connection strong { display:block; font-size:0.7rem; color:#f3f1e9 !important; }
.pj-connection span { display:block; margin-top:0.15rem; font-size:0.62rem; color:#a0aca8 !important; }

.pj-eyebrow { display:flex; align-items:center; gap:0.45rem; margin-bottom:0.55rem; color:#718078;
  font-size:0.62rem; letter-spacing:0.15em; font-weight:800; text-transform:uppercase; }
.pj-eyebrow-dot { width:7px; height:7px; border-radius:2px; background:var(--pj-primary); transform:rotate(45deg); }
.pj-subtitle { color:var(--pj-muted); font-size:0.88rem; margin:0.35rem 0 0.85rem 0; max-width:62ch; }
.pj-breadcrumb { display:flex; gap:0.55rem; align-items:center; font-size:0.72rem; color:#86908a; margin:0 0 0.85rem; }
.pj-breadcrumb strong { color:var(--pj-ink); font-weight:700; }

.pj-status-strip { display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;
  padding:0.85rem 1.05rem; margin:0 0 1.05rem; border:1px solid #c9d4c6; border-radius:12px; background:#e8f1e5; }
.pj-status-strip.warn { border-color:#e5d2b8; background:#fff6ea; }
.pj-status-strip.fail { border-color:#e5c4bc; background:#fff0ee; }
.pj-status-message { display:flex; align-items:center; gap:0.65rem; }
.pj-status-icon { display:grid; place-items:center; width:25px; height:25px; border-radius:50%;
  background:var(--pj-green); color:#fff; font-size:0.75rem; font-weight:800; }
.pj-status-strip.warn .pj-status-icon { background:var(--pj-amber); }
.pj-status-strip.fail .pj-status-icon { background:var(--pj-red); }
.pj-status-message strong { display:block; font-size:0.78rem; color:#22543f; }
.pj-status-strip.warn .pj-status-message strong { color:#8a4b16; }
.pj-status-strip.fail .pj-status-message strong { color:#8a2e26; }
.pj-status-message span { display:block; margin-top:0.15rem; font-size:0.72rem; color:#5f7567; }
.pj-strip-meta { display:flex; align-items:center; gap:0.9rem; flex-wrap:wrap; color:#5c7266; font-size:0.68rem; }
.pj-mini-dot { display:inline-block; width:6px; height:6px; border-radius:50%; margin-right:0.35rem; vertical-align:middle; }
.pj-mini-dot.blue { background:var(--pj-primary); }
.pj-mini-dot.green { background:var(--pj-green); }
.pj-mini-dot.amber { background:var(--pj-amber); }
.pj-mini-dot.red { background:var(--pj-red); }

.pj-kpi-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:0 0 1.05rem; }
@media (max-width:1100px){ .pj-kpi-grid{ grid-template-columns:repeat(2,minmax(0,1fr)); } }
.pj-kpi {
  padding:17px 18px 16px; border:1px solid var(--pj-line); border-radius:12px;
  background:#fbfaf7; box-shadow:0 4px 15px #4c554c08;
}
.pj-kpi-label { font-size:0.68rem; color:#7e8982; font-weight:700; letter-spacing:0.02em; }
.pj-kpi-value { display:block; margin-top:0.85rem; font-size:1.7rem; letter-spacing:-0.06em; font-weight:800; color:var(--pj-ink); }
.pj-kpi-detail { display:block; margin-top:0.25rem; color:#7b8780; font-size:0.65rem; }
.pj-kpi-detail.positive { color:var(--pj-green); }

.pj-card-eyebrow { color:#86928a; font-size:0.58rem; letter-spacing:0.15em; font-weight:800; text-transform:uppercase; margin:0 0 0.25rem; }
.pj-card-title { margin:0; font-size:1.05rem; letter-spacing:-0.035em; font-weight:800; color:var(--pj-ink); }
.pj-badge { display:inline-flex; align-items:center; gap:0.3rem; border-radius:999px; padding:0.28rem 0.55rem;
  font-size:0.65rem; font-weight:800; margin-right:0.25rem; white-space:nowrap; }
.pj-badge::before { content:""; width:5px; height:5px; border-radius:50%; background:currentColor; }
.pj-badge-ok, .pj-badge-green { background:var(--pj-green-bg); color:var(--pj-green); }
.pj-badge-warn, .pj-badge-amber { background:var(--pj-amber-bg); color:#b8672d; }
.pj-badge-fail, .pj-badge-red { background:var(--pj-red-bg); color:var(--pj-red); }
.pj-badge-info, .pj-badge-blue { background:var(--pj-primary-soft); color:var(--pj-primary); }
.pj-badge-mute { background:#efece4; color:#6d7973; }
.pj-badge-drop { background:var(--pj-amber-bg); color:var(--pj-amber); }

.pj-panel, .pj-preview {
  border:1px solid var(--pj-line); background:var(--pj-card); border-radius:12px;
  padding:0.95rem 1.05rem; margin:0.55rem 0 0.95rem; box-shadow:0 4px 15px #4c554c08;
}
.pj-preview { border-color:#cbd7ef; background:#eef3ff; }
.pj-panel-title { font-weight:800; color:var(--pj-ink); margin-bottom:0.45rem; }
.pj-kv { font-size:0.86rem; line-height:1.55; color:var(--pj-ink); }
.pj-kv code { background:#fff; border:1px solid #d1cdc4; padding:0.05rem 0.3rem; border-radius:4px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.78rem; }
.pj-section-label { font-size:0.62rem; font-weight:800; text-transform:uppercase; letter-spacing:0.13em;
  color:#86928a; margin:0.85rem 0 0.4rem; }
.pj-danger { border:1px solid #e5c4a8; background:var(--pj-amber-bg); color:#8a4b16; border-radius:10px;
  padding:0.65rem 0.85rem; margin:0.45rem 0 0.75rem; font-weight:700; font-size:0.84rem; }
.pj-detail-hero { display:flex; align-items:center; gap:0.7rem; margin:0.35rem 0 0.75rem; padding:0.8rem 0.85rem;
  border:1px solid #d9e1f5; border-radius:10px; background:#f1f5ff; }
.pj-detail-icon { display:grid; place-items:center; width:35px; height:35px; border-radius:9px;
  background:var(--pj-primary); color:#fff; font-weight:800; font-size:0.75rem; }
.pj-detail-hero strong { display:block; font-size:0.8rem; color:var(--pj-ink); }
.pj-detail-hero span { display:block; margin-top:0.2rem; color:#718078; font-size:0.65rem; }
.pj-op-pill { display:inline-block; padding:0.25rem 0.45rem; border-radius:5px; font-size:0.58rem; font-weight:800; }
.pj-op-pill.create { color:var(--pj-primary); background:var(--pj-primary-soft); }
.pj-op-pill.drop { color:var(--pj-amber); background:var(--pj-amber-bg); }
.pj-insight { position:relative; overflow:hidden; border-radius:13px; border:1px solid #202c34;
  background:#202c34; color:#f5f3ea; padding:1.25rem 1.2rem; margin:0.75rem 0 1rem; }
.pj-insight::after { content:""; position:absolute; right:-40px; top:-55px; width:160px; height:160px;
  border-radius:50%; background:var(--pj-lime); opacity:0.85; }
.pj-insight > * { position:relative; z-index:1; }
.pj-insight .pj-card-eyebrow { color:#aebbb4; }
.pj-insight h3 { margin:0.35rem 0 0.45rem; font-size:1.05rem; letter-spacing:-0.04em; color:#f5f3ea !important; }
.pj-insight p { margin:0 0 0.45rem; color:#b5c0bb; font-size:0.78rem; line-height:1.55; }
.pj-activity { border-top:1px solid #ebe8e1; padding:0.8rem 0; display:flex; gap:0.65rem; align-items:flex-start; }
.pj-activity-dot { width:8px; height:8px; margin-top:0.35rem; border-radius:2px; transform:rotate(45deg); flex:0 0 8px; }
.pj-activity-dot.green { background:var(--pj-green); }
.pj-activity-dot.amber { background:var(--pj-amber); }
.pj-activity-dot.red { background:var(--pj-red); }
.pj-activity strong { display:block; font-size:0.78rem; }
.pj-activity span { display:block; margin-top:0.2rem; color:#7f8a84; font-size:0.68rem; }
.pj-activity time { margin-left:auto; text-align:right; color:#8d9790; font-size:0.62rem; white-space:nowrap; }
.pj-activity time b { display:block; margin-top:0.2rem; color:#53615b; }
.pj-footer { display:flex; justify-content:space-between; gap:1rem; flex-wrap:wrap; padding:1.1rem 0 0;
  color:#8b958f; font-size:0.68rem; }
.pj-footer strong { color:#5e6d66; font-weight:800; }
.pj-step-bar { display:flex; flex-wrap:wrap; gap:0.4rem; margin:0.25rem 0 0.95rem; }
.pj-step { background:#fff; color:var(--pj-muted); border:1px solid #d1cdc4; border-radius:999px;
  padding:0.28rem 0.75rem; font-size:0.72rem; font-weight:700; }
.pj-step-active { background:var(--pj-primary-soft); color:var(--pj-primary); border-color:#b7c9f5; }
.pj-step-done { background:var(--pj-green-bg); color:var(--pj-green); border-color:#b7d9c7; }
.pj-code {
  margin-top:0.65rem; padding:0.75rem 0.85rem; border:1px solid #cbd7ef; border-radius:9px;
  background:#eef3ff; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.72rem;
  color:#40526f; line-height:1.6; white-space:pre-wrap;
}
.pj-menu-hint {
  display:inline-flex; align-items:center; gap:0.4rem; padding:0.35rem 0.65rem;
  border:1px solid #cac6bc; border-radius:9px; background:#faf9f5; color:#5f6d68;
  font-size:0.68rem; font-weight:700; margin-bottom:0;
}
.pj-menu-bar {
  display:flex; align-items:center; gap:0.65rem; flex-wrap:wrap;
  margin: 0 0 0.85rem 0; padding: 0.55rem 0.7rem;
  border:1px solid var(--pj-line); border-radius:12px; background:#fbfaf7cc;
}
.pj-sidebar-reopen {
  appearance:none; border:1px solid #c9c6bd; background:#fff; color:var(--pj-ink);
  border-radius:9px; padding:0.45rem 0.75rem; font-size:0.72rem; font-weight:800;
  cursor:pointer; box-shadow:0 4px 12px #202c3412;
}
.pj-sidebar-reopen:hover { border-color:#7f8d87; background:#f3f0e9; }
.pj-offline-panel {
  border:1px solid #e5c4a8; background:var(--pj-amber-bg); border-radius:12px;
  padding:0.95rem 1rem; margin:0.55rem 0 0.85rem; color:#8a4b16;
}
.pj-offline-panel strong { display:block; font-size:0.88rem; margin-bottom:0.35rem; }
.pj-offline-panel span { display:block; font-size:0.78rem; line-height:1.5; color:#9a5a28; }
.pj-offline-dot { margin-top:0.65rem; }
@media (prefers-reduced-motion:reduce){ *{ transition:none !important; animation:none !important; } }
:focus-visible { outline:3px solid #165dff !important; outline-offset:2px !important; }
</style>
"""
