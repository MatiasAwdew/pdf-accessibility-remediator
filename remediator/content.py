"""Map marked-content ids (MCIDs) to the text, font sizes and boxes they draw.

pdfplumber tags every char/image/path it extracts with the MCID of the
marked-content sequence it sits in, which lets us answer "what does this
struct element actually look like on the page?" -- the information a human
remediator uses to judge headings, figures and link targets.

All boxes are PDF user-space coordinates (origin bottom-left).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pdfplumber


@dataclass
class MarkedContent:
    text: str = ""
    sizes: list[float] = field(default_factory=list)
    bold: int = 0
    chars: int = 0
    bbox: tuple[float, float, float, float] | None = None
    has_image: bool = False
    has_paths: bool = False
    chars_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)

    def grow(self, x0, y0, x1, y1):
        if self.bbox is None:
            self.bbox = (x0, y0, x1, y1)
        else:
            a = self.bbox
            self.bbox = (min(a[0], x0), min(a[1], y0), max(a[2], x1), max(a[3], y1))

    @property
    def size(self) -> float | None:
        if not self.sizes:
            return None
        return Counter(round(s * 2) / 2 for s in self.sizes).most_common(1)[0][0]

    @property
    def is_bold(self) -> bool:
        return self.chars > 0 and self.bold / self.chars > 0.6


def _is_bold(fontname: str) -> bool:
    f = (fontname or "").lower()
    return any(t in f for t in ("bold", "black", "heavy", "semibold", ",b", "-bd"))


_CACHE: dict = {}


class ContentIndex:
    @classmethod
    def load(cls, path: str) -> "ContentIndex":
        """Cached: text extraction is the slowest step, and several stages need it."""
        import os
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
        if key not in _CACHE:
            if len(_CACHE) >= 8:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = cls(path)
        return _CACHE[key]

    def __init__(self, path: str):
        self.pages: list[dict[int, MarkedContent]] = []
        self.page_sizes: list[tuple[float, float]] = []
        self.untagged_text: list[int] = []  # chars drawn outside any marked content, per page
        self.tagged_text = 0                # visible chars inside marked content, whole doc
        self.body_size: float | None = None
        size_counter: Counter = Counter()

        with pdfplumber.open(path) as doc:
            for page in doc.pages:
                by_mcid: dict[int, MarkedContent] = defaultdict(MarkedContent)
                h = float(page.height)
                untagged = 0
                for ch in page.chars:
                    mcid = ch.get("mcid")
                    if mcid is None:
                        # tag set but no MCID = marked content inside a form XObject,
                        # whose MCIDs pdfplumber doesn't report; only text with no
                        # marked content at all is really untagged
                        if ch.get("text", "").strip() and ch.get("tag") is None:
                            untagged += 1
                        continue
                    mc = by_mcid[mcid]
                    self.tagged_text += bool(ch.get("text", "").strip())
                    if mc.chars_boxes and not mc.text.endswith(" "):
                        px0, py0, px1, py1 = mc.chars_boxes[-1]
                        size = float(ch["size"]) or 10
                        new_line = abs((h - ch["bottom"]) - py0) > size * 0.5 or ch["x0"] < px0 - 1
                        gap = ch["x0"] - px1 > size * 0.2
                        if new_line or gap:
                            mc.text += " "
                    mc.text += ch["text"]
                    mc.sizes.append(float(ch["size"]))
                    mc.chars += 1
                    mc.bold += _is_bold(ch.get("fontname", ""))
                    box = (ch["x0"], h - ch["bottom"], ch["x1"], h - ch["top"])
                    mc.chars_boxes.append(box)
                    mc.grow(*box)
                    if ch["text"].strip():
                        size_counter[round(float(ch["size"]) * 2) / 2] += 1
                for kind in ("images", "rects", "curves", "lines"):
                    for obj in getattr(page, kind, []):
                        mcid = obj.get("mcid")
                        if mcid is None:
                            continue
                        mc = by_mcid[mcid]
                        mc.grow(obj["x0"], h - obj["bottom"], obj["x1"], h - obj["top"])
                        if kind == "images":
                            mc.has_image = True
                        else:
                            mc.has_paths = True
                self.pages.append(dict(by_mcid))
                self.page_sizes.append((float(page.width), h))
                self.untagged_text.append(untagged)

        total = self.tagged_text + sum(self.untagged_text)
        self.untagged_share = sum(self.untagged_text) / total if total else 0.0
        if size_counter:
            self.body_size = size_counter.most_common(1)[0][0]

    def get(self, page: int | None, mcid: int) -> MarkedContent | None:
        if page is None or page >= len(self.pages):
            return None
        return self.pages[page].get(mcid)

    def describe(self, mcids) -> MarkedContent:
        """Merge several (page, mcid) pairs into one summary."""
        out = MarkedContent()
        for page, mcid in mcids:
            mc = self.get(page, mcid)
            if mc is None:
                continue
            out.text += mc.text
            out.sizes += mc.sizes
            out.bold += mc.bold
            out.chars += mc.chars
            out.has_image |= mc.has_image
            out.has_paths |= mc.has_paths
            out.chars_boxes += mc.chars_boxes
            if mc.bbox:
                out.grow(*mc.bbox)
        out.text = " ".join(out.text.split())
        return out
