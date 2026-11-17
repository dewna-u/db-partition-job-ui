#!/usr/bin/env python3
"""Render sql/framework/INVENTORY.md from sql/framework/framework_manifest.json.

  python scripts/framework_inventory.py           # print markdown
  python scripts/framework_inventory.py --write   # update INVENTORY.md
  python scripts/framework_inventory.py --check   # exit 1 if INVENTORY.md is stale

No database access.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework_manifest import iter_inventory_rows, load_manifest  # noqa: E402

INVENTORY_PATH = os.path.join(ROOT, "sql", "framework", "INVENTORY.md")

_OWNERSHIP_TITLES = (
    ("partops", "PARTOPS-OWNED OBJECTS (definition versioned in this repository)"),
    ("legacy_framework", "EXISTING / LEGACY DATABASE FRAMEWORK OBJECTS"),
    ("external", "EXTERNAL OBJECTS (pgAgent; optional, read-only)"),
)


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(manifest) -> str:
    rows = list(iter_inventory_rows(manifest))
    out = [
        "# PartOps database object inventory",
        "",
        "<!-- GENERATED from framework_manifest.json by scripts/framework_inventory.py — do not edit by hand. -->",
        "",
        "Expected framework version: `{}`. Required roles: {}.".format(
            manifest["framework_version"],
            ", ".join("`{}`".format(r["name"]) for r in manifest.get("roles") or []),
        ),
        "",
        "Status legend: `versioned` = definition in Git; `existing` = pre-existing object; "
        "`capture_required` = **authoritative definition NOT in Git** (capture from the approved "
        "reference DB; never invent); `legacy_must_not_run` = legacy runner PartOps never calls and "
        "that must not be scheduled alongside the realtime scheduler; `external_optional` = pgAgent.",
        "",
        "`in_git`: `yes` = authoritative definition in Git; `reconstructed` = table/type shape "
        "recreated by `bootstrap_partition_job_framework.sql` from the project specification "
        "(verify against the reference capture); `partial` / `no` as stated.",
        "",
    ]
    for ownership, title in _OWNERSHIP_TITLES:
        subset = [r for r in rows if r["ownership"] == ownership]
        if not subset:
            continue
        out += ["## " + title, "",
                "| kind | object | signature | status | in_git | required_by | required | security | owner | grants | called by | source |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in subset:
            out.append("| {} | `{}.{}` | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                r["kind"], r["schema"], r["name"], _cell(r["signature"] or "—"), r["status"],
                r["in_git"], r["required_by"], r["required"], r["security"], r["owner"],
                _cell(r["grants"]), _cell(r["called_by"]), _cell(r["source"])))
        out.append("")
        out.append("Purpose:")
        out.append("")
        for r in subset:
            out.append("- `{}` — {}".format(r["name"], _cell(r["purpose"])))
        out.append("")
    missing = [r for r in rows if r["status"] == "capture_required"]
    out += ["## Authoritative definitions missing from Git ({})".format(len(missing)), ""]
    for r in missing:
        out.append("- `{}.{}{}` — see `sql/framework/captured/{}.CAPTURE_REQUIRED.md`".format(
            r["schema"], r["name"], r["signature"], r["name"]))
    out += ["", "## Built-in functions and lock namespaces used", ""]
    out += ["- `{}`".format(b) for b in manifest.get("builtins_used") or []]
    for ns, meaning in (manifest.get("advisory_lock_namespaces") or {}).items():
        out.append("- advisory lock namespace `{}` — {}".format(ns, meaning))
    ext = manifest.get("extensions") or {}
    out.append("- extensions required: {}; optional: {}".format(
        ", ".join(ext.get("required") or []) or "none",
        ", ".join(ext.get("optional") or []) or "none"))
    out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render(load_manifest())
    if args.write:
        with open(INVENTORY_PATH, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return 0
    if args.check:
        try:
            with open(INVENTORY_PATH, encoding="utf-8") as handle:
                current = handle.read()
        except FileNotFoundError:
            print("INVENTORY.md missing")
            return 1
        if current != text:
            print("INVENTORY.md is stale; run scripts/framework_inventory.py --write")
            return 1
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
