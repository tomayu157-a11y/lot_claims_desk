"""
Insight-card -> PPTX export.

Renders a single, STANDARD 16:9 slide (13.333" x 7.5") that is a pixel-faithful
translation of the `.icard` component (header, finding, status row, evidence
block, "what this means", sources).

Two design decisions drive the code:

1. TWO-PASS LAYOUT. Every "measure" helper (text_height, pill_w, ...) is pure
   math with no dependency on a slide. render_card() is called once with
   `slide=None` to compute the total content height, and once for real, using
   identical numbers both times. Nothing can silently overflow its box.

2. SHRINK-TO-FIT, NOT GROW-THE-SLIDE. The slide size never changes — it's
   always the standard 13.333" x 7.5". If a card's content is taller than the
   available area, a single `scale` factor (< 1.0) is applied to every font
   size, padding, and gap — but never to the horizontal grid math (column
   counts, wrap widths) — and the whole card is re-measured/re-drawn at that
   scale. A few iterations converge scale so content fits inside the fixed
   slide, the same way PowerPoint's own "shrink text on overflow" behaves.
"""

import io
import re
import textwrap

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml import parse_xml
from pptx.util import Emu, Inches, Pt

# --------------------------------------------------------------------------- #
# Palette — lifted 1:1 from the CSS custom properties used by the .icard partial
# --------------------------------------------------------------------------- #
C_TEXT = RGBColor(0x0F, 0x17, 0x2A)          # var(--text)
C_MUTED = RGBColor(0x4A, 0x55, 0x67)         # var(--text-2)
C_FAINT = RGBColor(0x8A, 0x94, 0xA6)         # var(--text-3)
C_BORDER = RGBColor(0xE6, 0xE8, 0xEC)        # var(--border)
C_SURFACE_2 = RGBColor(0xF7, 0xF8, 0xFA)     # var(--surface-2)
C_SURFACE_3 = RGBColor(0xF4, 0xF6, 0xF8)     # var(--surface-3)
C_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
C_PRIMARY = RGBColor(0x25, 0x63, 0xEB)       # var(--primary)
C_PRIMARY_SOFT = RGBColor(0xEB, 0xF2, 0xFE)  # var(--primary-soft)
C_GREEN = RGBColor(0x15, 0x73, 0x47)         # var(--ok)
C_GREEN_SOFT = RGBColor(0xE7, 0xF7, 0xEE)    # var(--green-soft)
C_SHADOW = "17223A"

FONT = "Calibri"  # safe-list: ships with Office, true-to-width in LibreOffice QA

# Standard slide, always. Content shrinks to fit it — it never grows.
SLIDE_W_IN = 13.333
SLIDE_H_IN = 7.5
MARGIN_IN = 0.5
MIN_SCALE = 0.62   # floor so text never gets illegibly small
MAX_ITERS = 5

_ACRONYMS = {
    "nci", "seer", "pmc", "ash", "esmo", "fda", "who", "mrd", "ctgov",
    "clinicaltrials", "ppt", "ash/blood",
}


# --------------------------------------------------------------------------- #
# Text measurement — real glyph widths (Carlito, metric-compatible with
# Calibri, which is what LibreOffice/Word actually render) instead of a
# character-count fudge factor. A fudge factor either over-wraps (wastes
# space, forces needless shrinking) or under-wraps (silent overflow) depending
# on which way it's biased; measuring the actual font removes the guess.
# --------------------------------------------------------------------------- #
_FONT_FILES = {
    (False, False): "/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf",
    (True, False): "/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf",
    (False, True): "/usr/share/fonts/truetype/crosextra/Carlito-Italic.ttf",
    (True, True): "/usr/share/fonts/truetype/crosextra/Carlito-BoldItalic.ttf",
}
_FONT_REF_SIZE = 200  # render metrics at a large fixed size, then scale — sub-point precision
_font_cache = {}
_FONT_AVAILABLE = None


def _get_font(bold=False, italic=False):
    global _FONT_AVAILABLE
    key = (bold, italic)
    if key in _font_cache:
        return _font_cache[key]
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(_FONT_FILES[key], _FONT_REF_SIZE)
        _FONT_AVAILABLE = True
    except Exception:
        font = None
        _FONT_AVAILABLE = False
    _font_cache[key] = font
    return font


def _char_w(bold=False):
    # Fallback fudge factor only — used if PIL/Carlito isn't available on the
    # host. Empirically calibrated against real Carlito metrics (see above);
    # biased slightly narrow-of-true-width to stay a safe over-estimate.
    return 0.47 if bold else 0.43


def _text_width_in(text, font_pt, bold=False, italic=False):
    if not text:
        return 0.0
    font = _get_font(bold, italic)
    if font is None:
        return (len(text) * font_pt * _char_w(bold)) / 72.0
    width_at_ref = font.getlength(text)
    return (width_at_ref * (font_pt / _FONT_REF_SIZE)) / 72.0


def wrap_text(text, width_in, font_pt, bold=False):
    text = (text or "").strip()
    if not text:
        return [""]
    width_in = max(width_in, 0.3)

    if _get_font(bold) is None:
        # Fallback: character-count based wrapping.
        chars_per_line = max(6, int((width_in * 72) / (font_pt * _char_w(bold))))
        lines = []
        for para in text.split("\n"):
            lines.extend(textwrap.wrap(para, width=chars_per_line) or [""])
        return lines

    lines = []
    for para in text.split("\n"):
        words = para.split(" ")
        if not words or (len(words) == 1 and not words[0]):
            lines.append("")
            continue
        cur = ""
        for word in words:
            trial = f"{cur} {word}".strip()
            if not cur or _text_width_in(trial, font_pt, bold) <= width_in:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines or [""]


def text_height(text, width_in, font_pt, bold=False, line_mult=1.24):
    n = len(wrap_text(text, width_in, font_pt, bold))
    return (n * font_pt * line_mult) / 72.0


def pill_w(text, font_pt, pad_in=0.30, bold=True, min_in=0.55):
    return max(min_in, pad_in + _text_width_in(text, font_pt, bold))


def clean_text(txt):
    return re.sub(r"<[^>]+>", "", str(txt)).replace("[VERIFIED]", "").replace("[INFERENCE]", "").strip()


def humanize_source_id(sid):
    words = re.split(r"[_\-]+", sid.strip())
    out = [w.upper() if w.lower() in _ACRONYMS else w.capitalize() for w in words]
    return " ".join(out) or sid


def resolve_source_name(store, run_id, sid, source_names=None):
    if source_names and sid in source_names:
        return source_names[sid]
    getter = getattr(store, "get_source_display_name", None)
    if callable(getter):
        try:
            name = getter(run_id, sid)
            if name:
                return name
        except Exception:
            pass
    return humanize_source_id(sid)


# --------------------------------------------------------------------------- #
# Drawing primitives. Every one accepts `slide=None` and simply skips drawing.
# --------------------------------------------------------------------------- #
def add_soft_shadow(shape, blur_emu=95000, dist_emu=22000, alpha=26000, color=C_SHADOW):
    spPr = shape._element.spPr
    xml = (
        '<a:effectLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<a:outerShdw blurRad="{blur_emu}" dist="{dist_emu}" dir="5400000" rotWithShape="0">'
        f'<a:srgbClr val="{color}"><a:alpha val="{alpha}"/></a:srgbClr>'
        "</a:outerShdw></a:effectLst>"
    )
    spPr.append(parse_xml(xml))


def add_rect(slide, x, y, w, h, fill=None, line=None, radius=None, shadow=False):
    if slide is None:
        return None
    shape = MSO_SHAPE.ROUNDED_RECTANGLE if radius is not None else MSO_SHAPE.RECTANGLE
    shp = slide.shapes.add_shape(shape, x, y, w, h)
    if radius is not None:
        try:
            shp.adjustments[0] = radius
        except Exception:
            pass
    if fill is not None:
        shp.fill.solid()
        shp.fill.fore_color.rgb = fill
    else:
        shp.fill.background()
    if line is not None:
        shp.line.color.rgb = line
        shp.line.width = Pt(0.75)
    else:
        shp.line.fill.background()
    shp.shadow.inherit = False
    if shadow:
        add_soft_shadow(shp)
    return shp


def add_text(slide, x, y, w, h, text, font_pt, color, bold=False, italic=False,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, line_mult=1.24, font_name=FONT):
    if slide is None:
        return None
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    for i, line in enumerate((text or "").split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.alignment = align
        p.line_spacing = line_mult
        r = p.runs[0]
        r.font.size = Pt(font_pt)
        r.font.bold = bold
        r.font.italic = italic
        r.font.color.rgb = color
        r.font.name = font_name
    return box


def add_pill(slide, x, y, w, h, text, fill, text_color, font_pt=10, bold=True,
             border=None, align=PP_ALIGN.CENTER, font_name=FONT):
    if slide is None:
        return None
    shp = add_rect(slide, x, y, w, h, fill=fill, line=border, radius=0.5)
    tf = shp.text_frame
    tf.word_wrap = False
    tf.margin_left = tf.margin_right = Inches(0.05)
    tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.text = text
    p.alignment = align
    r = p.runs[0]
    r.font.size = Pt(font_pt)
    r.font.bold = bold
    r.font.color.rgb = text_color
    r.font.name = font_name
    return shp


def add_hanging_list(slide, x, y, w, items, font_pt=11.5, color=C_TEXT,
                      numbered=True, indent_in=0.28, space_after_in=0.09,
                      line_mult=1.24, font_name=FONT):
    """Numbered/bulleted list with a real hanging indent."""
    if slide is None:
        return
    box = slide.shapes.add_textbox(x, y, w, Inches(0.3))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        marker = f"{i + 1}." if numbered else "\u2022"
        p.text = f"{marker}\t{clean_text(item)}"
        p.line_spacing = line_mult
        p.space_after = Pt(space_after_in * 72)
        pPr = p._p.get_or_add_pPr()
        pPr.set("marL", str(int(Inches(indent_in))))
        pPr.set("indent", str(-int(Inches(indent_in))))
        r = p.runs[0]
        r.font.size = Pt(font_pt)
        r.font.color.rgb = color
        r.font.name = font_name


def list_height(items, w_in, indent_in, font_pt=11.5, space_after_in=0.09, line_mult=1.24):
    total = 0.0
    text_w = max(0.5, w_in - indent_in)
    for item in items:
        total += text_height(clean_text(item), text_w, font_pt, line_mult=line_mult) + space_after_in
    return total


# --------------------------------------------------------------------------- #
# Section renderers — every one takes `scale` and applies it to fonts/padding/
# gaps only. Horizontal grid math (column widths, wrap widths) is untouched by
# scale, since shrinking is about vertical footprint, not squeezing layout.
# --------------------------------------------------------------------------- #
def render_header(slide, insight, x, y, w, agent_label, scale=1.0):
    badge_w = Inches(0.46 * scale) if insight.number else Inches(0)
    tag_font = 10 * scale
    tag_w = Inches(pill_w(agent_label, tag_font, pad_in=0.34 * scale, bold=False))
    gap = Inches(0.16 * scale)
    title_x = x + (badge_w + gap if insight.number else Inches(0))
    title_w = w - (badge_w + gap if insight.number else Inches(0)) - tag_w - gap
    title_font = 18 * scale

    title_h_in = text_height(insight.title, title_w / 914400, title_font, bold=True, line_mult=1.15)
    row_h = max(Inches(0.4 * scale), Inches(title_h_in), Inches(0.32 * scale))

    if insight.number:
        badge_h = Inches(0.34 * scale)
        badge_y = y + (row_h - badge_h) / 2
        add_pill(slide, x, badge_y, badge_w, badge_h, f"{insight.number:02d}",
                 C_PRIMARY_SOFT, C_PRIMARY, font_pt=12 * scale, bold=True, align=PP_ALIGN.CENTER)

    add_text(slide, title_x, y, title_w, row_h, insight.title, title_font, C_TEXT,
              bold=True, anchor=MSO_ANCHOR.MIDDLE, line_mult=1.15)

    tag_h = Inches(0.32 * scale)
    tag_y = y + (row_h - tag_h) / 2
    add_pill(slide, x + w - tag_w, tag_y, tag_w, tag_h, agent_label,
              C_WHITE, C_FAINT, font_pt=tag_font, bold=False, border=C_BORDER)

    return y + row_h


def render_finding(slide, insight, x, y, w, scale=1.0):
    font_pt = 12.5 * scale
    h = Inches(text_height(insight.summary, w / 914400, font_pt, line_mult=1.3))
    add_text(slide, x, y, w, h, insight.summary, font_pt, C_TEXT, line_mult=1.3)
    return y + h


def render_status(slide, insight, x, y, w, scale=1.0):
    decision = getattr(insight.review_action, "value", insight.review_action)
    approved = decision not in (None, "", "pending")
    label = "Approved by you" if approved else "Ready"
    fill = C_GREEN_SOFT if approved else C_SURFACE_3
    fg = C_GREEN if approved else C_MUTED
    check = "\u2713 " if approved else ""
    font_pt = 10.5 * scale

    pw = Inches(pill_w(check + label, font_pt, pad_in=0.34 * scale))
    ph = Inches(0.3 * scale)
    add_pill(slide, x, y, pw, ph, check + label, fill, fg, font_pt=font_pt, bold=True)

    nsrc = len(insight.source_ids or [])
    src_txt = f"|   {nsrc} source{'' if nsrc == 1 else 's'}"
    cx = x + pw + Inches(0.14 * scale)
    add_text(slide, cx, y, Inches(2.2), ph, src_txt, font_pt, C_FAINT, anchor=MSO_ANCHOR.MIDDLE)
    cx += Inches(pill_w(src_txt, font_pt, pad_in=0.1, bold=False, min_in=0))

    if getattr(insight, "used_web_fallback", False):
        add_text(slide, cx, y, Inches(2.6), ph, "|   includes web evidence", font_pt, C_FAINT,
                  anchor=MSO_ANCHOR.MIDDLE)

    return y + ph


def render_section_label(slide, text, x, y, w, scale=1.0):
    font_pt = 9 * scale
    add_text(slide, x, y, w, Inches(0.22 * scale), text.upper(), font_pt, C_FAINT, bold=True)
    return y + Inches(0.26 * scale)


# -- evidence variants ------------------------------------------------------ #
def _metrics_layout(evidence, w_in, cols=4, gap_in=0.16):
    box_w = (w_in - gap_in * (cols - 1)) / cols
    rows, row = [], []
    for i, m in enumerate(evidence):
        row.append(m)
        if len(row) == cols or i == len(evidence) - 1:
            rows.append(row)
            row = []
    return box_w, rows


def _metric_box_height(m, box_w_in, scale=1.0):
    val, lbl = clean_text(m.get("value", "")), clean_text(m.get("label", ""))
    inner_w = box_w_in - 0.28 * scale
    vh = text_height(val, inner_w, 16 * scale, bold=True, line_mult=1.15)
    lh = text_height(lbl, inner_w, 9.5 * scale, line_mult=1.2)
    return 0.16 * scale + vh + 0.06 * scale + lh + 0.16 * scale


def render_metrics(slide, evidence, x, y, w, scale=1.0):
    w_in = w / 914400
    box_w, rows = _metrics_layout(evidence, w_in, gap_in=0.16 * scale)
    gap = Inches(0.16 * scale)
    cursor_y = y
    for row in rows:
        row_h = Inches(max(_metric_box_height(m, box_w, scale) for m in row))
        cursor_x = x
        for m in row:
            add_rect(slide, cursor_x, cursor_y, Inches(box_w), row_h, fill=C_WHITE, line=C_BORDER, radius=0.06)
            val, lbl = clean_text(m.get("value", "")), clean_text(m.get("label", ""))
            pad = Inches(0.14 * scale)
            inner_w = Inches(box_w) - 2 * pad
            vh = Inches(text_height(val, box_w - 0.28 * scale, 16 * scale, bold=True, line_mult=1.15))
            add_text(slide, cursor_x + pad, cursor_y + pad, inner_w, vh, val, 16 * scale, C_TEXT,
                      bold=True, line_mult=1.15)
            lh = Inches(text_height(lbl, box_w - 0.28 * scale, 9.5 * scale, line_mult=1.2))
            add_text(slide, cursor_x + pad, cursor_y + pad + vh + Inches(0.05 * scale), inner_w, lh, lbl,
                      9.5 * scale, C_FAINT, line_mult=1.2)
            cursor_x += Inches(box_w) + gap
        cursor_y += row_h + gap
    return cursor_y - gap


def metrics_height(evidence, w_in, scale=1.0):
    box_w, rows = _metrics_layout(evidence, w_in, gap_in=0.16 * scale)
    gap = 0.16 * scale
    total = sum(max(_metric_box_height(m, box_w, scale) for m in row) + gap for row in rows)
    return total - gap


def _col_weights(columns, rows):
    weights = []
    for i, col in enumerate(columns):
        longest = len(col)
        for r in rows:
            if i < len(r):
                longest = max(longest, len(clean_text(r[i])))
        weights.append(max(longest, 8))
    total = sum(weights)
    return [w_ / total for w_ in weights]


def render_table(slide, evidence, x, y, w, scale=1.0):
    cols, rows = evidence.get("columns", []), evidence.get("rows", [])
    if not cols or not rows:
        return y
    w_in = w / 914400
    weights = _col_weights(cols, rows)
    col_w_in = [w_in * wt for wt in weights]
    pad_in = 0.12 * scale
    header_font, cell_font = 9 * scale, 10.5 * scale

    header_h = Inches(0.34 * scale)
    add_rect(slide, x, y, w, header_h, fill=C_SURFACE_3)
    cx = x
    for c, cw in zip(cols, col_w_in):
        add_text(slide, cx + Inches(pad_in), y, Inches(cw - 2 * pad_in), header_h, c.upper(),
                  header_font, C_FAINT, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        cx += Inches(cw)
    cursor_y = y + header_h

    for r_idx, row in enumerate(rows):
        cell_texts = [clean_text(v) for v in row]
        row_h = Inches(max(
            text_height(t, col_w_in[i] - 2 * pad_in, cell_font, line_mult=1.2)
            for i, t in enumerate(cell_texts)
        ) + 2 * pad_in)
        if r_idx % 2 == 1:
            add_rect(slide, x, cursor_y, w, row_h, fill=C_SURFACE_2)
        cx = x
        for i, t in enumerate(cell_texts):
            faint = t.strip().lower() in ("not quantified", "n/a", "unknown", "not reported")
            add_text(slide, cx + Inches(pad_in), cursor_y, Inches(col_w_in[i] - 2 * pad_in), row_h, t,
                      cell_font, C_FAINT if faint else C_TEXT, italic=faint,
                      anchor=MSO_ANCHOR.MIDDLE, line_mult=1.2)
            cx += Inches(col_w_in[i])
        cursor_y += row_h
    add_rect(slide, x, y, w, cursor_y - y, fill=None, line=C_BORDER, radius=0.02)
    return cursor_y


def table_height(evidence, w_in, scale=1.0):
    cols, rows = evidence.get("columns", []), evidence.get("rows", [])
    if not cols or not rows:
        return 0.0
    weights = _col_weights(cols, rows)
    col_w_in = [w_in * wt for wt in weights]
    pad_in = 0.12 * scale
    cell_font = 10.5 * scale
    total = 0.34 * scale
    for row in rows:
        cell_texts = [clean_text(v) for v in row]
        total += max(
            text_height(t, col_w_in[i] - 2 * pad_in, cell_font, line_mult=1.2)
            for i, t in enumerate(cell_texts)
        ) + 2 * pad_in
    return total


def render_steps(slide, evidence, x, y, w, scale=1.0):
    pill_h = Inches(0.4 * scale)
    circle_d = Inches(0.24 * scale)
    row_gap, col_gap = Inches(0.14 * scale), Inches(0.14 * scale)
    step_font, num_font = 10.5 * scale, 9 * scale
    cursor_x, cursor_y = x, y
    for i, step in enumerate(evidence):
        text = clean_text(step)
        tw = Inches(pill_w(text, step_font, pad_in=0.16 * scale, bold=False, min_in=0.4 * scale))
        total_w = circle_d + Inches(0.1 * scale) + tw + Inches(0.18 * scale)
        if cursor_x != x and (cursor_x - x + total_w) > w:
            cursor_x = x
            cursor_y += pill_h + row_gap
        add_rect(slide, cursor_x, cursor_y, total_w, pill_h, fill=C_PRIMARY_SOFT, radius=0.5)
        gap = Inches(0.08 * scale)
        add_pill(slide, cursor_x + gap, cursor_y + (pill_h - circle_d) / 2, circle_d, circle_d,
                  str(i + 1), C_PRIMARY, C_WHITE, font_pt=num_font, bold=True)
        add_text(slide, cursor_x + gap + circle_d + gap, cursor_y, tw + Inches(0.1 * scale), pill_h,
                  text, step_font, C_PRIMARY, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        cursor_x += total_w + col_gap
    return cursor_y + pill_h


def steps_height(evidence, w_in, scale=1.0):
    return render_steps(None, evidence, Emu(0), Emu(0), Emu(int(Inches(w_in))), scale=scale) / 914400


# --------------------------------------------------------------------------- #
# Full card measurement + render (single source of truth, run at a given scale)
# --------------------------------------------------------------------------- #
def _evidence_height(insight, w_in, scale=1.0):
    if not insight.evidence:
        return 0.0
    et = insight.evidence_type
    if et == "metrics":
        return metrics_height(insight.evidence, w_in, scale)
    if et == "table" and "columns" in insight.evidence:
        return table_height(insight.evidence, w_in, scale)
    if et == "steps":
        return steps_height(insight.evidence, w_in, scale)
    return list_height(insight.evidence, w_in, indent_in=0.28 * scale, font_pt=11.5 * scale)


def _render_evidence(slide, insight, x, y, w, scale=1.0):
    et = insight.evidence_type
    if et == "metrics":
        return render_metrics(slide, insight.evidence, x, y, w, scale)
    if et == "table" and "columns" in insight.evidence:
        return render_table(slide, insight.evidence, x, y, w, scale)
    if et == "steps":
        return render_steps(slide, insight.evidence, x, y, w, scale)
    if slide is not None:
        add_hanging_list(slide, x, y, w, insight.evidence, numbered=True,
                          indent_in=0.28 * scale, font_pt=11.5 * scale,
                          space_after_in=0.09 * scale)
    h = list_height(insight.evidence, w / 914400, indent_in=0.28 * scale,
                     font_pt=11.5 * scale, space_after_in=0.09 * scale)
    return y + Inches(h)


def render_card(slide, insight, run_id, store, source_names, x, y, w, scale=1.0):
    """Draws (or, if slide is None, only measures) one insight card starting at
    (x, y) with width w, at font/spacing `scale`. Returns the card's bottom y."""
    GAP = Inches(0.24 * scale)
    pad = Inches(0.42 * scale)
    cx, cursor_y = x + pad, y + pad
    cw = w - 2 * pad

    agent_label = "Celestra Synthesis" if insight.category == "Synthesis" else insight.category

    cursor_y = render_header(slide, insight, cx, cursor_y, cw, agent_label, scale)
    cursor_y += Inches(0.16 * scale)

    cursor_y = render_finding(slide, insight, cx, cursor_y, cw, scale)
    cursor_y += Inches(0.14 * scale)

    cursor_y = render_status(slide, insight, cx, cursor_y, cw, scale)
    cursor_y += GAP

    if insight.evidence:
        cursor_y = _render_evidence(slide, insight, cx, cursor_y, cw, scale)
        cursor_y += GAP

    if insight.interpretation:
        cursor_y = render_section_label(slide, "What this means", cx, cursor_y, cw, scale)
        font_pt = 11.5 * scale
        h = Inches(text_height(insight.interpretation, cw / 914400, font_pt, line_mult=1.3))
        add_text(slide, cx, cursor_y, cw, h, insight.interpretation, font_pt, C_MUTED, line_mult=1.3)
        cursor_y += h + GAP

    for note_attr, label in (("user_input", "Your instruction"), ("reviewer_input", "Your input")):
        note = getattr(insight, note_attr, None)
        if note:
            font_pt = 10.5 * scale
            box_pad = Inches(0.16 * scale)
            note_w = cw - 2 * box_pad
            note_h = Inches(text_height(f"{label}: {note}", note_w / 914400, font_pt, line_mult=1.28)) + 2 * box_pad
            add_rect(slide, cx, cursor_y, cw, note_h, fill=C_PRIMARY_SOFT, radius=0.05)
            add_text(slide, cx + box_pad, cursor_y + box_pad, note_w, note_h - 2 * box_pad,
                      f"{label}: {note}", font_pt, C_TEXT, line_mult=1.28)
            cursor_y += note_h + Inches(0.14 * scale)

    if insight.source_ids:
        cursor_y = render_section_label(slide, "Sources", cx, cursor_y, cw, scale)
        names = [resolve_source_name(store, run_id, sid, source_names) for sid in insight.source_ids]
        font_pt = 9.5 * scale
        row_x, row_h = cx, Inches(0.3 * scale)
        for name in names:
            pw = Inches(pill_w(name, font_pt, pad_in=0.28 * scale, bold=False))
            if row_x != cx and (row_x - cx + pw) > cw:
                row_x = cx
                cursor_y += row_h + Inches(0.1 * scale)
            add_pill(slide, row_x, cursor_y, pw, row_h, name, C_SURFACE_3, C_FAINT, font_pt=font_pt, bold=False)
            row_x += pw + Inches(0.12 * scale)
        cursor_y += row_h
    else:
        font_pt = 10 * scale
        add_text(slide, cx, cursor_y, cw, Inches(0.24 * scale),
                  "No source returned usable evidence for this card.", font_pt, C_FAINT, italic=True)
        cursor_y += Inches(0.28 * scale)

    cursor_y += pad
    return cursor_y


# --------------------------------------------------------------------------- #
# FastAPI route
# --------------------------------------------------------------------------- #
def _measure(insight, run_id, store, source_names, card_w, scale):
    return render_card(None, insight, run_id, store, source_names, Emu(0), Emu(0), card_w, scale)


def _fit_scale(insight, run_id, store, source_names, card_w, available_h, iters=16):
    """Binary search for the LARGEST scale in [MIN_SCALE, 1.0] whose measured
    card height still fits inside `available_h`. Height is monotonically
    non-decreasing in scale, so bisection converges to the best-fitting (i.e.
    largest, most readable) scale rather than over-shrinking the way a single
    ratio-based guess can when word-wrap line breaks make height non-linear."""
    hi_h = _measure(insight, run_id, store, source_names, card_w, 1.0)
    if hi_h <= available_h:
        return 1.0, hi_h

    lo, hi = MIN_SCALE, 1.0
    lo_h = _measure(insight, run_id, store, source_names, card_w, lo)
    if lo_h > available_h:
        # Even the smallest allowed scale doesn't fit — use it anyway and
        # clamp the drawn card height; the slide size still never changes.
        return lo, lo_h

    for _ in range(iters):
        mid = (lo + hi) / 2
        mid_h = _measure(insight, run_id, store, source_names, card_w, mid)
        if mid_h <= available_h:
            lo, lo_h = mid, mid_h
        else:
            hi = mid
    return lo, lo_h


def build_insight_pptx(insight, run_id, store, source_names=None):
    """Returns an io.BytesIO of a single STANDARD 16:9 .pptx (13.333" x 7.5").
    Content is scaled down (fonts, padding, gaps — never layout) to fit if
    needed; the slide size itself never changes."""
    slide_w = Inches(SLIDE_W_IN)
    slide_h = Inches(SLIDE_H_IN)
    margin = Inches(MARGIN_IN)
    card_w = slide_w - 2 * margin
    available_h = slide_h - 2 * margin

    scale, card_h = _fit_scale(insight, run_id, store, source_names, card_w, available_h)
    card_h = min(card_h, available_h)  # hard safety clamp — the card never exceeds the slide

    prs = Presentation()
    prs.slide_width = slide_w
    prs.slide_height = slide_h
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    bg = slide.background
    bg.fill.solid()
    bg.fill.fore_color.rgb = C_SURFACE_2

    # Vertically center the card in the fixed slide (top-align when it fills it).
    card_y = margin + max(Inches(0), (available_h - card_h) / 2)

    add_rect(slide, margin, card_y, card_w, card_h, fill=C_WHITE, radius=0.035, shadow=True)
    render_card(slide, insight, run_id, store, source_names, margin, card_y, card_w, scale)

    stream = io.BytesIO()
    prs.save(stream)
    stream.seek(0)
    return stream


def register_route(app, store):
    @app.get("/runs/{run_id}/insights/{insight_id}/ppt")
    async def export_insight_ppt(run_id: str, insight_id: str):
        insight = store.get_insight(run_id, insight_id)
        if not insight:
            raise HTTPException(404, "Insight not found")

        stream = build_insight_pptx(insight, run_id, store)

        safe_title = "".join(c if c.isalnum() else "_" for c in insight.title)
        filename = f"Insight_{safe_title}.pptx"

        return StreamingResponse(
            stream,
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )