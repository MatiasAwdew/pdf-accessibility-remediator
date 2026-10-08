"""Heading structure: the "fix heading structure" step after AutoTag.

AutoTag's heading levels are often wrong (everything H1, or H2 -> H4 jumps).
Baseline: rank headings by visual style (font size, then bold), like a human
scanning the page. In "ai" mode Claude then reviews the whole outline using
cues font size can't see (numbering like 1.2.3, wording, document logic) plus
your past corrections. Finally skipped levels are removed so the outline nests.
"""

from __future__ import annotations

import re

from ..ai import AIUnavailable
from ..structure import HEADING_TYPES, STANDARD_TYPES

URL = re.compile(r"^(https?://|www\.)\S+$", re.I)
BLOCK_PARENTS = {"Document", "Part", "Sect", "Div", "Art", "BlockQuote", "NonStruct"}

# Publisher / Word style tags that are headings in all but name, but get
# role-mapped to <P> (so screen readers see no headings at all):
#   CHAP_TITLE, ArticleTitle, Title          -> document title (H1)
#   HEAD_1, Heading2, H_3, HEAD_3_after_HEAD_2 -> numbered heading levels
#   REF_HD, SectionHead, ...                  -> probably headings; Claude/you decide
TITLE_NAME = re.compile(r"^(?:(?:chap|chapter|art|article|doc|document|main|paper)[ _-]?)?title$", re.I)
NUMBERED_NAME = re.compile(r"^(?:head|heading|hd|h)[ _-]?(\d)(?![\d])", re.I)
LOOSE_NAME = re.compile(r"(?:head|heading|hd)$", re.I)
NOT_HEADING = re.compile(r"^(?:fig|figure|tb|tab|table|run|running|page|foot|header)", re.I)


def _publisher_tags(ctx, nodes) -> tuple[dict, list, bool]:
    """Promote the document title and clearly-named custom heading tags.
    Returns ({objgen: level hint}, loose candidates, whether a title became H1)."""
    tree = ctx.tree
    found = []
    title_seen = False
    for n in nodes:
        name = n.raw_type
        std = tree.std_type(n.elem)
        if std in HEADING_TYPES or NOT_HEADING.match(name):
            continue
        # PDF 2.0 <Title> (Adobe AutoTag uses it) or a publisher title tag
        if not title_seen and (std == "Title" or (name not in STANDARD_TYPES and TITLE_NAME.match(name))):
            found.append((n, 0))
            title_seen = True
            continue
        if name in STANDARD_TYPES:
            continue
        if (m := NUMBERED_NAME.match(name)):
            found.append((n, int(m.group(1))))
        elif LOOSE_NAME.search(name):
            found.append((n, None))
    has_title = any(k == 0 for _, k in found) or any(tree.std_type(n.elem) == "H1" for n in nodes)
    hints, loose, names = {}, [], set()
    for n, k in found:
        if k is None:
            loose.append(n)
            continue
        level = 1 if k == 0 else min(6, k + 1 if has_title else k)
        names.add(n.raw_type)
        tree.set_type(n.elem, f"H{level}")
        hints[n.elem.objgen] = level
    if hints:
        ctx.report.fixed("headings", f"Promoted {len(hints)} title/heading tag(s) that weren't real headings "
                         f"({', '.join(sorted(names)[:6])}) to H1-H6")
    return hints, loose, title_seen


def _style_key(mc) -> tuple[float, int] | None:
    if mc.size is None:
        return None
    return (mc.size, 1 if mc.is_bold else 0)


def _normalize(levels: list[int], first_h1: bool) -> list[int]:
    """Clamp so each heading is at most one level deeper than the previous."""
    out, prev = [], 0
    for i, lvl in enumerate(levels):
        if i == 0 and first_h1:
            lvl = 1
        lvl = max(1, min(lvl, prev + 1, 6))
        out.append(lvl)
        prev = lvl
    return out


def _context(mc, page, body, guess, prev_text) -> dict:
    return {"text": mc.text[:200], "size": mc.size, "bold": mc.is_bold, "body_size": body,
            "page": None if page is None else page + 1, "size_guess": guess,
            "previous_heading": (prev_text or "")[:120]}


def _paragraph_candidates(ctx, nodes, style_level, named=()) -> list[tuple]:
    """Short, larger-or-bold paragraphs that AutoTag missed as headings,
    plus custom tags whose name says "heading" (REF_HD, SectionHead...)."""
    tree, content = ctx.tree, ctx.content
    body = content.body_size
    if body is None:
        return []
    named_ids = {n.elem.objgen for n in named}
    out = []
    for n in nodes:
        if n.elem.objgen in named_ids:
            mc = content.describe(tree.all_mcids(n.elem))
            text = mc.text.strip()
            if text and len(text) <= 120:
                out.append((n, mc, style_level.get(mc.size, 2)))
            continue
        if tree.std_type(n.elem) != "P":
            continue
        # text inside tables, lists, links, TOCs etc. is never a heading
        if n.parent is not None and tree.std_type(n.parent.elem) not in BLOCK_PARENTS:
            continue
        mc = content.describe(tree.all_mcids(n.elem))
        text = mc.text.strip()
        # In a document the tool tagged itself, same-style lines must be treated
        # alike (agenda items of different lengths), and a multi-line title is fine.
        local = getattr(ctx, "locally_tagged", False)
        max_len = 90
        if local:
            max_len = 200 if (mc.size or 0) >= body * 1.3 else 120
        # "Recommendation:"-style labels can be headings; long lines ending in ":" are sentences
        ends_sentence = text.endswith((",", ";")) or (
            text.endswith(".") and not re.search(r"\b(Sr|Jr|Dr|Mr|Ms|Mrs|Inc|Co|St|No|Vol)\.$", text))
        if (not text or len(text) > max_len or ends_sentence or mc.size is None
                or (text.endswith(":") and len(text) > 40) or URL.match(text)
                or re.search(r"https?://|www\.", text)
                or re.fullmatch(r"[\d\s.,/$%-]+", text)):  # numbers alone: page numbers, totals
            continue
        bigger = mc.size >= body * 1.15
        if not (bigger or (mc.is_bold and mc.size >= body and len(text) < (120 if local else 60))):
            continue
        guess = style_level.get(mc.size)
        if guess is None:
            known = sorted(style_level.items(), key=lambda kv: abs(kv[0] - mc.size))
            guess = known[0][1] if known else 2
        out.append((n, mc, guess))
    return out


def run(ctx) -> None:
    cfg = ctx.config["headings"]
    if cfg["mode"] == "off":
        return
    tree, content, report = ctx.tree, ctx.content, ctx.report
    nodes = tree.build()
    hints, loose_named, title_promoted = _publisher_tags(ctx, nodes)
    heads = [n for n in nodes if tree.std_type(n.elem) in HEADING_TYPES | {"H"}]
    if not heads:
        report.review("headings", "Document has no heading tags")

    info, junk = [], 0
    for n in heads:
        t = tree.std_type(n.elem)
        cur = int(t[1]) if t in HEADING_TYPES else 1
        mc = content.describe(tree.all_mcids(n.elem))
        # Empty headings (AutoTag leftovers) and bare URLs are never headings
        if not mc.text.strip() or URL.match(mc.text.strip()):
            tree.set_type(n.elem, "P")
            junk += 1
            continue
        info.append((n, cur, mc))
    if junk:
        report.fixed("headings", f"Changed {junk} empty or URL-only heading tag(s) to P")

    # 1. baseline. Keep the outline the PDF already has unless it's degenerate
    #    (e.g. AutoTag made everything H1); then rank by visual style instead.
    #    Publisher tags promoted above use the level their name implies.
    levels = [hints.get(n.elem.objgen, cur) for n, cur, _ in info]
    own = [i for i, (n, _, _) in enumerate(info) if n.elem.objgen not in hints]
    if title_promoted and cfg.get("title_as_only_h1", True) and any(levels[i] == 1 for i in own):
        # Title is now the H1, so existing H1 sections become H2, H2 -> H3, ...
        for i in own:
            levels[i] = min(6, levels[i] + 1)
        report.fixed("headings", f"Made the document title the only H1; moved {len(own)} heading(s) down one level")
    degenerate = len(own) >= 3 and len({info[i][1] for i in own}) == 1
    if cfg["mode"] in ("fontsize", "ai") and (degenerate or cfg.get("always_rank_by_size")):
        styles = sorted({k for i in own if (k := _style_key(info[i][2]))}, reverse=True)
        rank = {k: r + 1 for r, k in enumerate(styles)}
        for i in own:
            k = _style_key(info[i][2])
            if k in rank:
                levels[i] = rank[k]
    size_guess = list(levels)
    style_level = {}  # font size -> level in the corrected outline, for guessing missed headings
    for (_, _, mc), lvl in zip(info, _normalize(levels, cfg["first_heading_h1"])):
        if mc.size:
            style_level.setdefault(mc.size, lvl)
    candidates = (_paragraph_candidates(ctx, nodes, style_level, loose_named)
                  if cfg["suggest_paragraph_headings"] else [])

    # 2. Claude reviews the whole outline
    reasons: dict[int, str] = {}
    not_heading: set[int] = set()
    ai_suggest: dict[int, int] = {}  # Claude's level for a heading we don't auto-change
    cand_levels = {j: g for j, (_, _, g) in enumerate(candidates)}
    classify = getattr(ctx.ai, "classify_headings", None) if cfg["mode"] == "ai" else None
    source = "size" if degenerate else "structure"
    ai_decided = False
    order = {n.elem.objgen: k for k, n in enumerate(nodes)}
    if classify and (info or candidates):
        entries = []
        for i, (n, cur, mc) in enumerate(info):
            entries.append((order[n.elem.objgen], {"index": i, "text": mc.text[:150], "size": mc.size,
                            "bold": mc.is_bold, "page": (tree.page_of(n.elem) or 0) + 1,
                            "current_tag": f"H{cur}", "tag_name": n.raw_type, "size_guess": size_guess[i]}))
        for j, (n, mc, g) in enumerate(candidates):
            entries.append((order[n.elem.objgen], {"index": len(info) + j, "text": mc.text[:150],
                            "size": mc.size, "bold": mc.is_bold, "page": (tree.page_of(n.elem) or 0) + 1,
                            "current_tag": "P", "tag_name": n.raw_type, "size_guess": g}))
        entries = [e for _, e in sorted(entries, key=lambda x: x[0])]
        try:
            decisions = classify(entries, content.body_size)
        except AIUnavailable as e:
            ctx.ai = None
            decisions = None
            report.error("headings", str(e))
        except Exception as e:
            decisions = None
            report.info("headings", f"AI heading review failed, used document structure only: {e}")
        if decisions:
            source, ai_decided = "AI", True
            for i in range(len(info)):
                d = decisions.get(i)
                if d is None:
                    continue
                reasons[i] = d.reason
                trusted = degenerate or info[i][0].elem.objgen in hints
                if 1 <= d.level <= 6 and trusted:
                    levels[i] = d.level
                elif 1 <= d.level <= 6 and d.level != levels[i]:
                    # the PDF's own outline is valid: benchmark showed Claude overriding
                    # correct publisher levels, so this is only a suggestion
                    ai_suggest[i] = d.level
                elif d.level == 0:
                    not_heading.add(i)
            for j in range(len(candidates)):
                d = decisions.get(len(info) + j)
                if d is not None:
                    cand_levels[j] = d.level if 0 <= d.level <= 6 else cand_levels[j]
                    reasons[len(info) + j] = d.reason

    # 3. Masthead: a few headings before the real H1 (journal name, "Research
    #    Article") become text, instead of forcing them to H1 and pushing the
    #    whole outline down a level.
    masthead: set[int] = set()
    live = [i for i in range(len(info)) if i not in not_heading]
    first_h1 = next((k for k, i in enumerate(live) if levels[i] == 1), None)
    if first_h1 and first_h1 <= 3:
        masthead = set(live[:first_h1])

    # 4. Final outline = kept headings + promotions both heuristics and Claude
    #    agree on, in reading order, with skipped levels removed.
    auto_promote = ai_decided and cfg.get("auto_promote_when_ai_agrees", True)
    # Only visibly larger text is tagged automatically. Bold text at body size
    # (memo labels, agenda times) stays a suggestion: on a real remediated agenda
    # the specialist left all of those as text.
    body = content.body_size or 0
    min_size = body * cfg.get("auto_promote_min_size_ratio", 1.15)
    # A document the tool tagged itself has no headings to fix, so layout-based
    # headings are tagged (Claude's "not a heading" verdicts still win).
    local = getattr(ctx, "locally_tagged", False)
    promoted = [j for j in range(len(candidates))
                if cand_levels.get(j) and ((auto_promote and (candidates[j][1].size or 0) >= min_size) or local)]
    outline = sorted([(order[info[i][0].elem.objgen], "h", i) for i in range(len(info)) if i not in masthead]
                     + [(order[candidates[j][0].elem.objgen], "c", j) for j in promoted])
    final = _normalize([levels[i] if kind == "h" else cand_levels[i] for _, kind, i in outline],
                       cfg["first_heading_h1"])
    final_head = {i: lv for (_, kind, i), lv in zip(outline, final) if kind == "h"}
    final_cand = {i: lv for (_, kind, i), lv in zip(outline, final) if kind == "c"}

    prev_text = ""
    for i, (n, cur, mc) in enumerate(info):
        page = tree.page_of(n.elem)
        label = (mc.text[:60] + "...") if len(mc.text) > 60 else mc.text
        why = f" ({reasons[i]})" if i in reasons else ""
        context = _context(mc, page, content.body_size, size_guess[i], prev_text)
        prev_text = mc.text
        if i in masthead:
            tree.set_type(n.elem, "P")
            report.review("headings", f'"{label}" comes before the document title - changed H{cur} to P{why}',
                          page=page, elem=n.elem, proposal={"field": "demote", "old": f"H{cur}", "value": "P",
                                                            "applied": True, "context": context})
            continue
        new = final_head[i]
        if i in not_heading:
            report.review("headings", f'"{label}" may not be a heading -> suggest P{why}', page=page,
                          elem=n.elem, proposal={"field": "demote", "old": f"H{new}", "value": "P",
                                                 "applied": False, "context": context})
        if i in ai_suggest and ai_suggest[i] != new:
            report.review("headings", f'"{label}": Claude suggests H{ai_suggest[i]} instead of H{new}{why}',
                          page=page, elem=n.elem,
                          proposal={"field": "level", "old": new, "value": ai_suggest[i], "applied": False,
                                    "context": context})
        if tree.std_type(n.elem) == f"H{new}":
            continue
        tree.set_type(n.elem, f"H{new}")
        report.review("headings", f'"{label}": H{cur} -> H{new} [{source}]{why}', page=page, elem=n.elem,
                      proposal={"field": "level", "old": cur, "value": new, "context": context})

    for j, (n, mc, _) in enumerate(candidates):
        lvl = final_cand.get(j) or cand_levels.get(j, 0)
        if not lvl:
            continue  # Claude says it isn't a heading
        text = mc.text.strip()
        why = f" ({reasons[len(info) + j]})" if len(info) + j in reasons else ""
        page = tree.page_of(n.elem)
        context = _context(mc, page, content.body_size, lvl, "")
        if j in final_cand:
            tree.set_type(n.elem, f"H{lvl}")
            basis = "Claude agrees" if ai_decided else "larger/bold text in a document the tool tagged"
            report.review("headings", f'Tagged "{text[:60]}" as H{lvl} ({basis}){why}',
                          page=page, elem=n.elem,
                          proposal={"field": "promote", "old": "P", "value": f"H{lvl}", "applied": True,
                                    "context": context})
        else:
            report.review("headings", f'Possible missed heading "{text[:60]}" -> suggest H{lvl}{why}',
                          page=page, elem=n.elem,
                          proposal={"field": "promote", "old": "P", "value": f"H{lvl}", "applied": False,
                                    "context": context})
