"""Icon-based, horizontally scrolling lineage view and page header for Streamlit (rendered with st.html).

Uses the official Microsoft icons you downloaded, expected at  assets/icons/  next to app.py:
  * business-central.svg  (Dynamics 365 icons)
  * foundry-agent.svg     (Azure architecture icons, Microsoft Foundry)
File names are matched loosely (e.g. "Business-Central.svg", "Foundry-Agent-Service.svg").

Icons are embedded as <img src="data:..."> because st.html sanitises markup with DOMPurify, which removes inline
<svg> tags but keeps <img> data URIs. Microsoft's rules are respected: icons are never cropped, flipped, rotated or
distorted (fixed square box, object-fit: contain) and the product name is shown next to them.
"""
import base64
import html
import os
import re
from functools import lru_cache
from pathlib import Path

import lineage as lin

ICON_DIR = Path(os.getenv("LINEAGE_ICON_DIR", Path(__file__).resolve().parent / "assets" / "icons"))
ICON_PX = 30       # icon box on each card
CARD_PX = 166      # card width

# role -> loose file-name keys (letters and digits only, lower case), most specific first
_KEYS = {
    "business_central": ["businesscentral"],
    "agent": ["foundryagentservice", "foundryagent", "agentservice", "aiagent", "foundry"],
}
LABELS = {"business_central": "Dynamics 365 Business Central", "agent": "Microsoft Foundry agent"}


@lru_cache(maxsize=None)
def _official(role: str):
    """data: URI of the user's official icon for this role, or None."""
    if not ICON_DIR.is_dir():
        return None
    files = [p for p in ICON_DIR.iterdir() if p.suffix.lower() in (".svg", ".png")]
    files.sort(key=lambda p: (p.suffix.lower() != ".svg", p.name.lower()))
    for key in _KEYS[role]:
        for p in files:
            if key in re.sub(r"[^a-z0-9]", "", p.stem.lower()):
                mime = "image/svg+xml" if p.suffix.lower() == ".svg" else "image/png"
                return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    return None


def icon_status() -> list:
    """[(label, found)] so the app can tell the user whether official icons were picked up."""
    return [(LABELS[r], _official(r) is not None) for r in ("business_central", "agent")]


# ---- neutral line icons for non-Microsoft steps (own drawings, stroke based) ------------------------------------------------
_GLYPHS = {
    "email": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    "shield": '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
    "gear": '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1'
            'M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
    "person": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-4 3.5-6 7-6s7 2 7 6"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
    "cross": '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    "warn": '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
}


@lru_cache(maxsize=None)
def _glyph(name: str, color: str) -> str:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{_GLYPHS[name]}</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


# ---- which icon belongs to which lineage node ---------------------------------------------------------------
_OK, _STOP, _WARN = {"✅", "📦"}, {"🚫", "⛔", "❌"}, {"⚠️", "⚠"}


def icon_for(node: dict) -> dict:
    """{'uri', 'tag', 'label'} for one lineage node. The leading emoji of the node title selects the role."""
    emoji, _, label = node["title"].partition(" ")
    if emoji == "🤖":
        return {"uri": _official("agent"), "tag": "Microsoft Foundry agent", "label": label}
    if emoji == "🔧":
        return {"uri": _official("business_central"), "tag": "Business Central API", "label": label}
    if emoji == "📧":
        return {"uri": _glyph("email", "#1A73E8"), "tag": "Customer email", "label": label}
    if emoji == "🛡️":
        return {"uri": _glyph("shield", "#2E7D32"), "tag": "Validation (orchestrator)", "label": label}
    if emoji == "⚖️":
        return {"uri": _glyph("gear", "#1A73E8"), "tag": "Orchestrator (this app)", "label": label}
    if emoji == "🧑‍⚖️":
        return {"uri": _glyph("person", "#EF6C00"), "tag": "Human triage", "label": label}
    if emoji in _OK:   # a document now exists (or was simulated) in Business Central
        return {"uri": _official("business_central") or _glyph("check", "#2E7D32"),
                "tag": "Business Central", "label": label}
    if emoji in _STOP:
        return {"uri": _glyph("cross", "#C62828"), "tag": "Outcome", "label": label}
    if emoji in _WARN:
        return {"uri": _glyph("warn", "#EF6C00"), "tag": "Outcome", "label": label}
    return {"uri": _glyph("info", "#1A73E8"), "tag": "Outcome", "label": node["title"]}


# ---- HTML rendering (inline styles only: nothing depends on a <style> block surviving sanitising) -----------
_STATE_LABEL = {"ok": "✓ OK", "warn": "⚠ Attention", "error": "✕ Failed", "info": "ℹ Info"}


def _e(value, limit: int) -> str:
    return html.escape(lin.compact(value, limit), quote=True)


def _img(uri, px: int) -> str:
    if not uri:
        return ""
    return f'<img src="{uri}" alt="" style="width:{px}px;height:{px}px;flex:none;object-fit:contain;">'


def _card(n: dict) -> str:
    ic, c = icon_for(n), lin.STYLE[n["state"]]
    tip = html.escape(f"IN: {lin.compact(n['input'], 500)} | OUT: {lin.compact(n['output'], 500)}", quote=True)
    ms = f" · {n['ms']} ms" if n.get("ms") is not None else ""
    small = "font-size:10.5px;margin-top:2px;word-break:break-word;overflow-wrap:anywhere;"
    return (
        f'<div title="{tip}" style="width:{CARD_PX}px;box-sizing:border-box;background:{c["fill"]};'
        f'border:2px solid {c["stroke"]};border-radius:8px;padding:5px 7px;color:{lin.TEXT_COLOR};line-height:1.25;">'
        f'<div style="display:flex;align-items:center;gap:6px;">'
        f'{_img(ic["uri"], ICON_PX)}'
        f'<div style="min-width:0;"><div style="font-weight:600;font-size:12px;word-break:break-word;">'
        f'{_e(ic["label"], 34)}</div><div style="font-size:9.5px;opacity:.75;">{html.escape(ic["tag"])}</div></div></div>'
        f'<div style="{small}"><b>IN</b> {_e(n["input"], 52)}</div>'
        f'<div style="{small}"><b>OUT</b> {_e(n["output"], 52)}</div>'
        f'<div style="font-size:9.5px;margin-top:3px;opacity:.8;">{_STATE_LABEL[n["state"]]}{ms}</div></div>')


def _arrow(label: str) -> str:
    lab = (f'<div style="font-size:9px;line-height:1.1;text-align:center;max-width:52px;">{_e(label, 22)}</div>'
           if label else "")
    return ('<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;'
            f'min-width:28px;opacity:.8;">{lab}<div style="font-size:18px;line-height:1;">→</div></div>')


def _lane(title: str, inner: str) -> str:
    return ('<div style="display:flex;flex-direction:column;border:1px dashed rgba(128,128,128,.55);'
            'border-radius:10px;padding:3px 6px 6px;background:rgba(128,128,128,.07);">'
            '<div style="font-size:9.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;'
            f'opacity:.75;margin:0 0 3px 2px;">{html.escape(title)}</div>'
            f'<div style="display:flex;align-items:center;gap:2px;">{inner}</div></div>')


def _legend() -> str:
    def chip(color_key, text):
        c = lin.STYLE[color_key]
        return (f'<span style="display:inline-flex;align-items:center;gap:4px;"><span style="width:11px;height:11px;'
                f'border-radius:3px;background:{c["fill"]};border:2px solid {c["stroke"]};display:inline-block;"></span>'
                f'{text}</span>')
    items = []
    for role in ("agent", "business_central"):
        uri = _official(role)
        if uri:
            items.append(f'<span style="display:inline-flex;align-items:center;gap:4px;">{_img(uri, 18)}'
                         f'{html.escape(LABELS[role])}</span>')
    items += [chip("ok", "OK"), chip("warn", "Attention / simulated"), chip("error", "Failed / blocked"),
              chip("info", "Input / orchestrator")]
    return ('<div style="display:flex;flex-wrap:wrap;gap:6px 16px;font-size:11px;margin-top:2px;opacity:.9;">'
            + "".join(items) + "</div>")


def to_html(model: dict) -> str:
    """Horizontal, scrollable lineage: lanes (who) containing cards (what), arrows labelled with the data passed."""
    edge_label = {e["to"]: e["label"] for e in model["edges"]}
    lane_title = dict(lin.LANES)
    groups = []
    for n in model["nodes"]:
        if groups and groups[-1][0] == n["lane"]:
            groups[-1][1].append(n)
        else:
            groups.append((n["lane"], [n]))
    parts = []
    for gi, (lane, nodes) in enumerate(groups):
        if gi:
            parts.append(_arrow(edge_label.get(nodes[0]["id"], "")))
        inner = []
        for ni, n in enumerate(nodes):
            if ni:
                inner.append(_arrow(edge_label.get(n["id"], "")))
            inner.append(_card(n))
        parts.append(_lane(lane_title.get(lane, lane), "".join(inner)))
    return ('<div style="overflow-x:auto;padding:2px 0 8px;"><div style="display:flex;align-items:stretch;'
            f'gap:2px;width:max-content;min-width:100%;">{"".join(parts)}</div></div>' + _legend())


def header_html(title: str, subtitle: str) -> str:
    """Page title with the Foundry and Business Central icons beside it."""
    icons = "".join(_img(_official(r), 46) for r in ("agent", "business_central"))
    return ('<div style="display:flex;align-items:center;gap:14px;padding:0 0 8px;margin-bottom:4px;'
            'border-bottom:1px solid rgba(128,128,128,.25);">'
            f'<div style="display:flex;align-items:center;gap:8px;">{icons}</div>'
            f'<div><div style="font-size:2rem;font-weight:700;line-height:1.15;">{html.escape(title)}</div>'
            f'<div style="font-size:.95rem;opacity:.72;margin-top:2px;">{html.escape(subtitle)}</div></div></div>')
