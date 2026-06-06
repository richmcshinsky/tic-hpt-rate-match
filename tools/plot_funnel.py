"""
plot_funnel.py
==============
Render the pipeline funnel as a static SVG straight from the stats the run
emits, so the picture in the README can never drift from the data.

    python tools/plot_funnel.py            # reads out/unified_rates.csv(.stats.json)
    python tools/plot_funnel.py --stats path/to.stats.json --out docs/pipeline_funnel.svg

No third-party dependency on purpose: the SVG is plain string templating so a
reviewer needs nothing beyond the stdlib (and the committed SVG renders without
running this at all).
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Palette: blues = matched, oranges = unmatched, green = corroboration, neutral chrome.
C_TIC      = "#3b6ea5"
C_HPT      = "#2a9d8f"
C_STRICT   = "#2f5d8a"
C_RELAXED  = "#7fb0d3"
C_HPT_ONLY = "#e07a3e"
C_TIC_ONLY = "#f2b885"
C_CORRO    = "#2f8f5b"
C_CORRO_BG = "#eaf5ee"
C_CHIP     = "#f4f7fa"
C_CHIP_BD  = "#cfd9e2"
C_INK      = "#243845"
C_MUTE     = "#6b7b87"
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def _chip(x, y, w, h, accent, title, value, note=None):
    parts = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" '
        f'fill="{C_CHIP}" stroke="{C_CHIP_BD}" stroke-width="1"/>',
        f'<rect x="{x}" y="{y}" width="5" height="{h}" rx="2" fill="{accent}"/>',
        f'<text x="{x + 16}" y="{y + 19}" font-size="11" fill="{C_MUTE}">{title}</text>',
        f'<text x="{x + 16}" y="{y + 39}" font-size="18" font-weight="600" fill="{C_INK}">{value}</text>',
    ]
    if note:
        parts.append(
            f'<text x="{x + 16}" y="{y + h + 14}" font-size="10" fill="{C_MUTE}">{note}</text>'
        )
    return "\n".join(parts)


def _arrow(x1, y, x2):
    return (
        f'<line x1="{x1}" y1="{y}" x2="{x2 - 7}" y2="{y}" stroke="{C_MUTE}" stroke-width="1.5"/>'
        f'<path d="M{x2 - 7},{y - 4} L{x2},{y} L{x2 - 7},{y + 4} Z" fill="{C_MUTE}"/>'
    )


def build_svg(s: dict, corro_example: "str | None") -> str:
    W, H = 820, 520
    el: list[str] = []
    el.append(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="{FONT}">')
    el.append(f'<rect x="0" y="0" width="{W}" height="{H}" rx="10" fill="#ffffff"/>')

    el.append(f'<text x="28" y="34" font-size="17" font-weight="700" fill="{C_INK}">'
              f'Pipeline funnel — TiC × HPT → {s["output_rows"]} unified rate rows</text>')
    el.append(f'<text x="28" y="52" font-size="11" fill="{C_MUTE}">'
              f'generated from out/unified_rates.csv.stats.json</text>')

    xs = [28, 222, 416]
    cw, ch = 168, 50

    # TiC lane
    ty = 84
    el.append(f'<text x="28" y="{ty - 8}" font-size="12" font-weight="600" fill="{C_TIC}">TiC (payer)</text>')
    el.append(_chip(xs[0], ty, cw, ch, C_TIC, "input rows", f'{s["tic_rows_input"]:,}'))
    el.append(_arrow(xs[0] + cw, ty + ch / 2, xs[1]))
    el.append(_chip(xs[1], ty, cw, ch, C_TIC, "after filters", f'{s["tic_rows_after_filter"]:,}',
                    note=f'−{s["tic_rows_dropped_percentage"]} percentage'))
    el.append(_arrow(xs[1] + cw, ty + ch / 2, xs[2]))
    el.append(_chip(xs[2], ty, cw, ch, C_TIC, "match groups", f'{s["tic_match_groups"]:,}'))

    # HPT lane
    hy = 178
    el.append(f'<text x="28" y="{hy - 8}" font-size="12" font-weight="600" fill="{C_HPT}">HPT (hospital)</text>')
    el.append(_chip(xs[0], hy, cw, ch, C_HPT, "input rows", f'{s["hpt_rows_input"]:,}'))
    el.append(_arrow(xs[0] + cw, hy + ch / 2, xs[1]))
    el.append(_chip(xs[1], hy, cw, ch, C_HPT, "eligible postings", f'{s["hpt_rows_match_eligible"]:,}',
                    note=(f'−{s["hpt_rows_dropped_local"]} LOCAL · −{s["hpt_rows_dropped_no_dollar"]} no-$ · '
                          f'−{s["hpt_rows_dropped_no_payer"]:,} payer')))
    el.append(_arrow(xs[1] + cw, hy + ch / 2, xs[2]))
    el.append(_chip(xs[2], hy, cw, ch, C_HPT, "match groups", f'{s["hpt_match_groups"]:,}'))

    # Central match pill
    cxc = W / 2
    pw, ph, py = 230, 40, 262
    px = (W - pw) / 2
    el.append(f'<line x1="{cxc}" y1="{hy + ch + 8}" x2="{cxc}" y2="{py}" stroke="{C_MUTE}" stroke-width="1.2"/>')
    el.append(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="20" fill="{C_INK}"/>')
    el.append(f'<text x="{cxc}" y="{py + 18}" font-size="12.5" font-weight="600" fill="#ffffff" '
              f'text-anchor="middle">two-pass match</text>')
    el.append(f'<text x="{cxc}" y="{py + 32}" font-size="10" fill="#c7d3dc" text-anchor="middle">'
              f'Pass 1 strict {s["matches_pass1"]} · Pass 2 relaxed {s["matches_pass2"]}</text>')

    # Outcomes stacked bar (to scale)
    segs = [
        ("strict (pass 1)", s["matches_pass1"], C_STRICT, "#ffffff"),
        ("relaxed (pass 2)", s["matches_pass2"], C_RELAXED, C_INK),
        ("hpt_only", s["unmatched_hpt"], C_HPT_ONLY, "#ffffff"),
        ("tic_only", s["unmatched_tic"], C_TIC_ONLY, C_INK),
    ]
    total = s["output_rows"]
    bx, by, bw, bh = 28, 350, W - 56, 46
    el.append(f'<text x="{bx}" y="{by - 10}" font-size="12" font-weight="600" '
              f'fill="{C_INK}">unified output — {total} rows</text>')
    el.append(f'<line x1="{cxc}" y1="{py + ph}" x2="{cxc}" y2="{by - 7}" stroke="{C_MUTE}" stroke-width="1.2"/>')
    el.append(f'<path d="M{cxc},{by} l-6,-8 h12 Z" fill="{C_MUTE}"/>')
    cx = bx
    matched_w = 0.0
    for label, val, fill, txt in segs:
        seg_w = bw * val / total
        el.append(f'<rect x="{cx:.1f}" y="{by}" width="{seg_w:.1f}" height="{bh}" fill="{fill}"/>')
        if seg_w > 26:
            el.append(f'<text x="{cx + seg_w / 2:.1f}" y="{by + bh / 2 + 6}" font-size="15" '
                      f'font-weight="700" fill="{txt}" text-anchor="middle">{val}</text>')
        if label.startswith(("strict", "relaxed")):
            matched_w += seg_w
        cx += seg_w

    # Pass 3 — rate-value corroboration band, bracketed under the matched region.
    blocks = s.get("rate_corroborated_blocks", 0)
    rows = s.get("rate_corroborated_rows", 0)
    ry = by + bh + 24
    rh = 50
    # bracket from the matched portion of the bar down to the band
    bracket_x = bx + matched_w / 2
    el.append(f'<line x1="{bracket_x:.1f}" y1="{by + bh}" x2="{bracket_x:.1f}" y2="{ry - 8}" '
              f'stroke="{C_CORRO}" stroke-width="1.2" stroke-dasharray="3 2"/>')
    el.append(f'<path d="M{bracket_x:.1f},{ry} l-6,-8 h12 Z" fill="{C_CORRO}"/>')
    el.append(f'<rect x="{bx}" y="{ry}" width="{bw}" height="{rh}" rx="8" '
              f'fill="{C_CORRO_BG}" stroke="{C_CORRO}" stroke-width="1"/>')
    el.append(f'<rect x="{bx}" y="{ry}" width="5" height="{rh}" rx="2" fill="{C_CORRO}"/>')
    el.append(f'<text x="{bx + 18}" y="{ry + 21}" font-size="12.5" font-weight="700" fill="{C_CORRO}">'
              f'Pass 3 · rate-value corroboration</text>')
    el.append(f'<text x="{bx + 18}" y="{ry + 39}" font-size="11.5" fill="{C_INK}">'
              f'{blocks} blocks / {rows} rows flagged — exact cross-side $ agreement, '
              f'independent of billing class</text>')
    if corro_example:
        el.append(f'<text x="{bx + bw - 14}" y="{ry + 30}" font-size="11" font-weight="600" '
                  f'fill="{C_CORRO}" text-anchor="end">{corro_example}</text>')

    # Legend (below the corroboration band, so the bracket connector stays clear)
    ly = ry + rh + 26
    slot = (W - 56) / len(segs)
    for i, (label, val, fill, _) in enumerate(segs):
        lx = bx + i * slot
        el.append(f'<rect x="{lx}" y="{ly - 11}" width="13" height="13" rx="3" fill="{fill}"/>')
        el.append(f'<text x="{lx + 19}" y="{ly}" font-size="12" fill="{C_INK}">{label} · {val}</text>')

    el.append("</svg>")
    return "\n".join(el)


def _find_example(csv_path: Path) -> "str | None":
    """Pull the brief's UHC × 43239 corroborated value from the CSV, so the
    figure's headline example is data-driven and cannot silently go stale."""
    if not csv_path.exists():
        return None
    try:
        with open(csv_path, newline="") as fh:
            for r in csv.DictReader(fh):
                if (str(r.get("code")) == "43239"
                        and r.get("payer_canonical") == "unitedhealthcare"
                        and str(r.get("rate_value_corroborated")).lower() == "true"
                        and r.get("corroborated_values")):
                    val = r["corroborated_values"].split(";")[0].strip()
                    return f"e.g. Mount Sinai × UHC × 43239 = ${float(val):,.0f}"
    except Exception:
        return None
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render the pipeline funnel SVG from stats JSON.")
    ap.add_argument("--stats", type=Path, default=REPO / "out" / "unified_rates.csv.stats.json")
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "pipeline_funnel.svg")
    args = ap.parse_args(argv)

    stats = json.loads(args.stats.read_text())
    csv_path = args.stats.parent / args.stats.name.replace(".stats.json", "")
    example = _find_example(csv_path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_svg(stats, example))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
