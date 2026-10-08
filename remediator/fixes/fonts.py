"""Embed fonts that aren't embedded (Acrobat Preflight: "Embed missing fonts").

PDF/UA requires every font used for text to be embedded. Acrobat's AutoTag adds
invisible 1pt spaces in a non-embedded Times-Roman, so almost every AutoTagged
file fails this check. Like Preflight's fixup, we embed the matching installed
font (Times New Roman for Times, Arial for Helvetica, Courier New for Courier),
subset to the characters actually used, with widths taken from that font so
the Widths array and the font program agree (a PDF/UA requirement).

Only simple fonts are handled (Type1 / TrueType with a Latin encoding).
Symbol / ZapfDingbats and composite fonts are reported for Acrobat instead.
"""

from __future__ import annotations

import io
import os
import random
import re
import string

import pikepdf
from pikepdf import Array, Dictionary, Name

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
# Linux (the hosted server): system and user font folders
FONT_DIRS = [FONT_DIR] if os.name == "nt" else \
    ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts")]

# Metric-compatible open fonts, for where Microsoft fonts aren't installed
OPEN_EQUIVALENT = {
    "arial": "liberationsans", "helvetica": "liberationsans", "timesnewroman": "liberationserif",
    "times": "liberationserif", "timesnewromanps": "liberationserif", "couriernew": "liberationmono",
    "courier": "liberationmono", "couriernewps": "liberationmono", "calibri": "carlito", "cambria": "caladea",
}
STYLE_WORDS = {"bold": "bold", "italic": "italic", "oblique": "italic", "bolditalic": "bolditalic",
               "boldoblique": "bolditalic", "regular": "regular", "roman": "regular", "": "regular"}

# normalized PDF font name -> Windows font file
SUBSTITUTES = {
    "times-roman": "times", "times-bold": "timesbd", "times-italic": "timesi", "times-bolditalic": "timesbi",
    "timesnewroman": "times", "timesnewromanpsmt": "times", "timesnewroman,bold": "timesbd",
    "timesnewromanps-boldmt": "timesbd", "timesnewroman,italic": "timesi", "timesnewromanps-italicmt": "timesi",
    "timesnewroman,bolditalic": "timesbi", "timesnewromanps-bolditalicmt": "timesbi",
    "helvetica": "arial", "helvetica-bold": "arialbd", "helvetica-oblique": "ariali",
    "helvetica-boldoblique": "arialbi", "arial": "arial", "arialmt": "arial", "arial,bold": "arialbd",
    "arial-boldmt": "arialbd", "arial,italic": "ariali", "arial-italicmt": "ariali",
    "arial,bolditalic": "arialbi", "arial-bolditalicmt": "arialbi",
    "courier": "cour", "courier-bold": "courbd", "courier-oblique": "couri", "courier-boldoblique": "courbi",
    "couriernew": "cour", "couriernewpsmt": "cour", "couriernew,bold": "courbd", "couriernewps-boldmt": "courbd",
    "couriernew,italic": "couri", "couriernew,bolditalic": "courbi",
}
TEXT_SHOW = {"Tj", "TJ", "'", '"'}


_INSTALLED: dict | None = None


def installed_fonts() -> dict[str, str]:
    """normalized PostScript / full / family+style name -> font file, for installed TrueType fonts."""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    from fontTools.ttLib import TTFont
    idx: dict[str, str] = {}
    paths = []
    for folder in FONT_DIRS:
        for root, _, files in (os.walk(folder) if os.path.isdir(folder) else []):
            paths += [os.path.join(root, f) for f in files if f.lower().endswith(".ttf")]
    for path in sorted(paths):
        try:
            tt = TTFont(path, lazy=True)
            names = tt["name"]
            family = (names.getDebugName(1) or "").replace(" ", "")
            style = (names.getDebugName(2) or "").replace(" ", "")
            for key in (names.getDebugName(6), names.getDebugName(4), family + "-" + style, family + "," + style,
                        family if style.lower() in ("regular", "normal", "") else None):
                if key:
                    idx.setdefault(key.replace(" ", "").lower(), path)
            tt.close()
        except Exception:
            continue
    _INSTALLED = idx
    return idx


def _find_font_file(base: str) -> str | None:
    sub = SUBSTITUTES.get(_norm(base))
    if sub:
        p = os.path.join(FONT_DIR, sub + ".ttf")
        if os.path.exists(p):
            return p
    name = _norm(base)
    idx = installed_fonts()
    for cand in (name, name.replace(",", "-"), name.replace("mt", ""), name.split(",")[0] if "," not in name else None):
        if cand and cand in idx:
            return idx[cand]
    # Not installed (Arial/Calibri on the Linux server): use a metric-compatible
    # open font with the same character widths, so the layout doesn't move.
    parts = re.split(r"[,-]", name, maxsplit=1)
    family = re.sub(r"(ps)?mt$", "", parts[0])
    style = re.sub(r"(ps)?mt$", "", parts[1]) if len(parts) > 1 else ""
    alt = OPEN_EQUIVALENT.get(family)
    if alt:
        st = STYLE_WORDS.get(style, "regular")
        for cand in (f"{alt}-{st}", alt if st == "regular" else None):
            if cand and cand in idx:
                return idx[cand]
    return None


def _norm(base: str) -> str:
    name = base.lstrip("/")
    if len(name) > 7 and name[6] == "+":  # subset tag
        name = name[7:]
    return name.replace(" ", "").lower()


def _is_embedded(font: Dictionary) -> bool:
    desc = font.get("/FontDescriptor")
    return isinstance(desc, Dictionary) and any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))


def _code_to_unicode(font: Dictionary) -> dict[int, str]:
    """Byte code -> character, from the font's encoding (WinAnsi / MacRoman / Differences)."""
    enc = font.get("/Encoding")
    base = "cp1252"
    diffs = None
    if isinstance(enc, Name) and enc == Name.MacRomanEncoding:
        base = "mac_roman"
    elif isinstance(enc, Dictionary):
        if enc.get("/BaseEncoding") == Name.MacRomanEncoding:
            base = "mac_roman"
        diffs = enc.get("/Differences")
    table = {}
    for code in range(32, 256):
        try:
            ch = bytes([code]).decode(base)
        except UnicodeDecodeError:
            continue
        if ch.isprintable() or ch == " ":
            table[code] = ch
    if diffs is not None:
        from fontTools import agl
        code = 0
        for item in diffs:
            if isinstance(item, int):
                code = int(item)
            else:
                uni = agl.toUnicode(str(item).lstrip("/"))
                if uni:
                    table[code] = uni
                code += 1
    return table


def _used_codes(pdf) -> dict[tuple, set[int]]:
    """objgen of font -> byte codes drawn with it (pages + their form XObjects)."""
    used: dict[tuple, set[int]] = {}
    seen_streams = set()

    def scan(stream_owner, resources):
        fonts = resources.get("/Font") if isinstance(resources, Dictionary) else None
        fonts = fonts if isinstance(fonts, Dictionary) else Dictionary()
        cur = None
        try:
            instructions = pikepdf.parse_content_stream(stream_owner)
        except Exception:
            return
        for ins in instructions:
            op = str(getattr(ins, "operator", ""))
            if op == "Tf":
                f = fonts.get(str(ins.operands[0]))
                cur = f.objgen if isinstance(f, Dictionary) and f.is_indirect else None
            elif op in TEXT_SHOW and cur is not None:
                bucket = used.setdefault(cur, set())
                for operand in ins.operands:
                    items = operand if isinstance(operand, Array) else [operand]
                    for s in items:
                        if isinstance(s, pikepdf.String):
                            bucket.update(bytes(s))
            elif op == "Do" and isinstance(resources, Dictionary):
                xo = (resources.get("/XObject") or {}).get(str(ins.operands[0]))
                if isinstance(xo, pikepdf.Stream) and xo.get("/Subtype") == Name.Form and xo.objgen not in seen_streams:
                    seen_streams.add(xo.objgen)
                    scan(xo, xo.get("/Resources") or resources)

    for page in pdf.pages:
        scan(page, page.obj.get("/Resources"))
        # annotation appearances (stamps, form fields) have their own content
        for annot in page.obj.get("/Annots") or []:
            ap = annot.get("/AP") if isinstance(annot, Dictionary) else None
            normal = ap.get("/N") if isinstance(ap, Dictionary) else None
            streams = [normal] if isinstance(normal, pikepdf.Stream) else                 [v for _, v in normal.items()] if isinstance(normal, Dictionary) else []
            for st in streams:
                if isinstance(st, pikepdf.Stream) and st.objgen not in seen_streams:
                    seen_streams.add(st.objgen)
                    scan(st, st.get("/Resources") or Dictionary())
    return used


def font_resource_dicts(pdf):
    """Every /Font resource dict: pages, and form XObjects nested inside them
    (logos and letterheads often carry their own fonts there)."""
    seen = set()

    def walk(res, depth):
        if not isinstance(res, Dictionary) or depth > 8:
            return
        fonts = res.get("/Font")
        if isinstance(fonts, Dictionary):
            # inline (direct) font dicts belong to one resource dict, so they can't
            # repeat; don't dedupe them by id() - Python reuses ids of freed wrappers,
            # which silently skipped whole pages
            if not fonts.is_indirect:
                yield fonts
            elif fonts.objgen not in seen:
                seen.add(fonts.objgen)
                yield fonts
        xobjects = res.get("/XObject")
        if isinstance(xobjects, Dictionary):
            for _, xo in xobjects.items():
                if isinstance(xo, pikepdf.Stream) and xo.get("/Subtype") == Name.Form:
                    if xo.objgen in seen:
                        continue
                    seen.add(xo.objgen)
                    yield from walk(xo.get("/Resources"), depth + 1)

    for page in pdf.pages:
        yield from walk(page.obj.get("/Resources"), 0)
        # annotation appearances (stamps, form fields) draw text with their own fonts
        for annot in page.obj.get("/Annots") or []:
            ap = annot.get("/AP") if isinstance(annot, Dictionary) else None
            normal = ap.get("/N") if isinstance(ap, Dictionary) else None
            streams = [normal] if isinstance(normal, pikepdf.Stream) else                 [v for _, v in normal.items()] if isinstance(normal, Dictionary) else []
            for st in streams:
                if isinstance(st, pikepdf.Stream) and st.objgen not in seen:
                    seen.add(st.objgen)
                    yield from walk(st.get("/Resources"), 1)


def _subset_tag() -> str:
    return "".join(random.choices(string.ascii_uppercase, k=6))


def _embed(pdf, font: Dictionary, ttf_path: str, codes: set[int]) -> list[int]:
    """Rewrite font as an embedded, subset TrueType. Returns codes the font can't draw."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    table = _code_to_unicode(font)
    codes = {c for c in codes if c in table} or {32}
    tt = TTFont(ttf_path)
    if tt["OS/2"].fsType & 0x0002:  # restricted license: embedding not allowed
        raise PermissionError(f"{os.path.basename(ttf_path)} does not allow embedding")
    cmap = tt.getBestCmap()
    missing = [c for c in codes if ord(table[c][0]) not in cmap]
    upm = tt["head"].unitsPerEm
    hmtx = tt["hmtx"]

    first, last = min(codes), max(codes)
    widths = []
    for code in range(first, last + 1):
        ch = table.get(code)
        glyph = cmap.get(ord(ch[0])) if ch else None
        widths.append(round(hmtx[glyph][0] * 1000 / upm) if glyph else 0)

    opts = subset.Options()
    opts.glyph_names = True          # keep post names: code -> name -> glyph
    opts.notdef_outline = True
    opts.name_IDs = ["*"]
    opts.drop_tables += ["DSIG"]
    sub = subset.Subsetter(opts)
    sub.populate(unicodes={ord(table[c][0]) for c in codes})
    sub.subset(tt)
    buf = io.BytesIO()
    tt.save(buf)
    data = buf.getvalue()

    ps_name = tt["name"].getDebugName(6) or os.path.splitext(os.path.basename(ttf_path))[0]
    font_name = Name("/" + _subset_tag() + "+" + ps_name)
    head, hhea, os2, post = tt["head"], tt["hhea"], tt["OS/2"], tt["post"]
    scale = 1000 / upm
    flags = 32  # nonsymbolic
    if post.isFixedPitch:
        flags |= 1
    if "times" in ttf_path.lower():
        flags |= 2  # serif
    if post.italicAngle:
        flags |= 64
    stream = pdf.make_stream(data)
    stream.Length1 = len(data)
    desc = pdf.make_indirect(Dictionary(
        Type=Name.FontDescriptor, FontName=font_name, Flags=flags,
        FontBBox=Array([round(v * scale) for v in (head.xMin, head.yMin, head.xMax, head.yMax)]),
        ItalicAngle=float(post.italicAngle), Ascent=round(hhea.ascent * scale), Descent=round(hhea.descent * scale),
        CapHeight=round(getattr(os2, "sCapHeight", hhea.ascent) * scale), StemV=80, FontFile2=stream))

    font.Subtype = Name.TrueType
    font.BaseFont = font_name
    font.FirstChar = first
    font.LastChar = last
    font.Widths = Array(widths)
    font.FontDescriptor = desc
    enc = font.get("/Encoding")
    if enc is None:
        font.Encoding = Name.WinAnsiEncoding
    elif isinstance(enc, Dictionary) and "/BaseEncoding" not in enc:
        # PDF/UA 7.21.6-2: non-symbolic TrueType needs a WinAnsi/MacRoman base
        enc.BaseEncoding = Name.WinAnsiEncoding
    if "/ToUnicode" not in font:  # reliable text extraction for screen readers
        lines = [f"<{c:02X}> <{''.join(f'{ord(u):04X}' for u in table[c])}>" for c in sorted(codes)]
        cmap_src = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                    "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                    "/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
                    "1 begincodespacerange <00> <FF> endcodespacerange\n"
                    f"{len(lines)} beginbfchar\n" + "\n".join(lines) + "\nendbfchar\n"
                    "endcmap CMapName currentdict /CMap defineresource pop end end\n")
        font.ToUnicode = pdf.make_stream(cmap_src.encode("ascii"))
    return missing


def run(ctx) -> None:
    if not ctx.config["fonts"]["embed_missing"]:
        return
    pdf, report = ctx.pdf, ctx.report
    # Some producers write font dicts inline in /Resources; make them indirect
    # objects so usage can be tracked per font and the edit applies everywhere.
    for fonts in list(font_resource_dicts(pdf)):
        if isinstance(fonts, Dictionary):
            for key in list(fonts.keys()):
                if isinstance(fonts[key], Dictionary) and not fonts[key].is_indirect:
                    fonts[key] = pdf.make_indirect(fonts[key])
    used = _used_codes(pdf)
    done, embedded, skipped = set(), [], []
    for fonts in font_resource_dicts(pdf):
        for _, font in fonts.items():
            if not isinstance(font, Dictionary) or not font.is_indirect or font.objgen in done:
                continue
            done.add(font.objgen)
            if _is_embedded(font) or font.get("/Subtype") not in (Name.Type1, Name.TrueType):
                continue
            base = str(font.get("/BaseFont", "?"))
            path = _find_font_file(base)
            if not path:
                skipped.append(base.lstrip("/"))
                continue
            try:
                missing = _embed(pdf, font, path, used.get(font.objgen, set()))
            except Exception as e:
                skipped.append(f"{base.lstrip('/')} ({e})")
                continue
            embedded.append(f"{base.lstrip('/')} -> {os.path.basename(path)}")
            if missing:
                report.review("fonts", f"{base.lstrip('/')}: {len(missing)} character(s) not in "
                              f"{os.path.basename(path)}; check they still display")
    if embedded:
        report.fixed("fonts", f"Embedded {len(embedded)} font(s): {', '.join(sorted(set(embedded)))}")
    for s in skipped:
        report.error("fonts", f"Font not embedded: {s} - use Acrobat Preflight 'Embed fonts'")
