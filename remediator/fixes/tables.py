"""Tables: missing TH, missing Scope, missing Summary, irregular grids."""

from __future__ import annotations

from pikepdf import Array, Name, String

from ..structure import is_struct_elem, as_list


def _rows(tree, table) -> list:
    rows = []
    for kid in as_list(table.get("/K")):
        if not is_struct_elem(kid):
            continue
        t = tree.std_type(kid)
        if t == "TR":
            rows.append(kid)
        elif t in ("THead", "TBody", "TFoot"):
            rows.extend(k for k in as_list(kid.get("/K")) if is_struct_elem(k) and tree.std_type(k) == "TR")
    return rows


def _cells(tree, row) -> list:
    return [k for k in as_list(row.get("/K")) if is_struct_elem(k) and tree.std_type(k) in ("TH", "TD")]


def _span(tree, cell, key) -> int:
    v = tree.get_attr(cell, "/Table", key)
    try:
        return max(1, int(v)) if v is not None else 1
    except (TypeError, ValueError):
        return 1


def _row_widths(tree, rows) -> list[int]:
    """Effective column count per row, accounting for ColSpan and RowSpan."""
    carry = [0] * len(rows)  # columns occupied by rowspans from above
    widths = []
    for r, row in enumerate(rows):
        w = carry[r]
        for c in _cells(tree, row):
            cs, rs = _span(tree, c, "/ColSpan"), _span(tree, c, "/RowSpan")
            w += cs
            for below in range(r + 1, min(len(rows), r + rs)):
                carry[below] += cs
        widths.append(w)
    return widths


def _context_text(ctx, node) -> str:
    """Heading / caption / paragraph just before the table, for the summary."""
    tree, content = ctx.tree, ctx.content
    cap = [k for k in as_list(node.elem.get("/K")) if is_struct_elem(k) and tree.std_type(k) == "Caption"]
    parts = [content.describe(tree.all_mcids(c)).text for c in cap]
    if node.parent is not None:
        sibs = node.parent.children
        i = next((k for k, s in enumerate(sibs) if s is node), 0)
        for s in sibs[max(0, i - 2):i]:
            parts.append(content.describe(tree.all_mcids(s.elem)).text)
    return " | ".join(" ".join(p.split())[:200] for p in parts if p.strip())


def _ai_summary(ctx, node, headers, sample, n_rows, n_cols) -> str | None:
    summarize = getattr(ctx.ai, "summarize_table", None)
    if summarize is None or not (headers or sample):
        return None
    from ..ai import AIUnavailable
    try:
        return summarize(headers, sample, _context_text(ctx, node), n_rows, n_cols)
    except AIUnavailable as e:
        ctx.ai = None
        ctx.report.error("tables", str(e))
        return None
    except Exception as e:
        ctx.report.info("tables", f"AI table summary failed, used column headers: {e}")
        return None


def _fragments(tree, nodes) -> list[list]:
    """Runs of 3+ sibling tables with at most 3 rows each: AutoTag split one table
    (often one piece per data row). Screen readers then announce dozens of tiny
    tables, and each piece's 'header' row is really data."""
    groups = []
    for n in nodes:
        run = []
        for ch in n.children:
            if tree.std_type(ch.elem) == "Table" and len(_rows(tree, ch.elem)) <= 3:
                run.append(ch)
                continue
            if len(run) >= 3:
                groups.append(run)
            run = []
        if len(run) >= 3:
            groups.append(run)
    return groups


def run(ctx) -> None:
    tree, content, report, cfg = ctx.tree, ctx.content, ctx.report, ctx.config["tables"]
    nodes = tree.build()
    fragment_ids = set()
    for group in _fragments(tree, nodes):
        pages = sorted({(tree.page_of(g.elem) or 0) + 1 for g in group})
        where = f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}-{pages[-1]}"
        report.error("tables", f"AutoTag split one table into {len(group)} separate tables on {where}; screen "
                     "readers announce each piece as its own table. Merge them into one table in Acrobat "
                     "(Reading Order tool: draw one Table region, or fix in the Table Editor)",
                     page=tree.page_of(group[0].elem), elem=group[0].elem)
        fragment_ids.update(g.elem.objgen for g in group)
    for n in nodes:
        if tree.std_type(n.elem) != "Table":
            continue
        table, page = n.elem, tree.page_of(n.elem)
        rows = _rows(tree, table)
        if not rows:
            report.error("tables", "Table has no TR rows - rebuild it in the Table Editor", page=page, elem=table)
            continue
        grid = [_cells(tree, r) for r in rows]
        has_th = any(tree.std_type(c) == "TH" for row in grid for c in row)
        has_td = any(tree.std_type(c) == "TD" for row in grid for c in row)

        if not has_th and cfg["promote_first_row_headers"] and grid[0]:
            for c in grid[0]:
                tree.set_type(c, "TH")
            has_th = True
            report.review("tables", f"No header cells: made the first row ({len(grid[0])} cells) TH "
                          "- verify, or set row headers instead", page=page, elem=table)

        # AutoTag scatters TH through data rows (row numbers, names, short codes).
        # The header block is the run of all-TH rows at the top (tables can have
        # 2-3 header rows, e.g. spanning group headings over column headings);
        # stray TH cells below it are data.
        n_head = 0
        while n_head < len(grid) and grid[n_head] and all(tree.std_type(c) == "TH" for c in grid[n_head]):
            n_head += 1
        if n_head == len(grid):  # everything TH: AutoTag gave up, assume one header row
            n_head = 1
        if cfg["data_row_headers"] == "demote" and 0 < n_head < len(grid):
            stray = [c for row in grid[n_head:] for c in row if tree.std_type(c) == "TH"]
            for c in stray:
                tree.set_type(c, "TD")
            if stray:
                rows_word = "row 1" if n_head == 1 else f"rows 1-{n_head}"
                report.review("tables", f"Header {rows_word}: changed {len(stray)} header cell(s) in data rows "
                              "to TD (set data_row_headers = \"keep\" if you use row headers)",
                              page=page, elem=table)
        has_td = any(tree.std_type(c) == "TD" for row in grid for c in row)
        if not has_td and table.objgen not in fragment_ids:  # pieces are covered by the split-table error
            report.error("tables", "Table has header cells but no data (TD) cells - "
                         "is this really a table?", page=page, elem=table)

        if cfg["fill_scope"]:
            scoped = 0
            for r, row in enumerate(grid):
                for c_i, cell in enumerate(row):
                    if tree.std_type(cell) != "TH" or tree.get_attr(cell, "/Table", "/Scope") is not None:
                        continue
                    scope = "/Column" if r == 0 else ("/Row" if c_i == 0 else "/Column")
                    tree.set_attr(cell, "/Table", "/Scope", Name(scope))
                    scoped += 1
            if scoped:
                report.fixed("tables", f"Set Scope on {scoped} header cell(s)", page=page)

        widths = _row_widths(tree, rows)
        if len(set(widths)) > 1 and cfg["pad_short_rows"]:
            target, added = max(widths), 0
            for row, w in zip(rows, widths):
                extra = [tree.new_elem("TD", row, tree.page_of(row)) for _ in range(target - w)]
                if extra:
                    row.K = Array(list(as_list(row.get("/K"))) + extra)
                    added += len(extra)
            widths = _row_widths(tree, rows)
            report.review("tables", f"Irregular table: added {added} empty cell(s) so every row has {target} "
                          "columns - check they sit in the right columns (or use ColSpan)", page=page, elem=table)
        if len(set(widths)) > 1:
            report.error("tables", f"Irregular table: rows have {sorted(set(widths))} columns "
                         "(fix row/col spans in the Table Editor)", page=page, elem=table)

        def cell_text(c):
            return " ".join(content.describe(tree.all_mcids(c)).text.split())[:60]

        # Pieces of a split table still need a summary (Acrobat's checker fails any
        # table without one), but a size or purpose summary would mislead: say it
        # continues the table and which row it holds.
        if cfg["add_summary"] and tree.get_attr(table, "/Table", "/Summary") is None \
                and table.objgen in fragment_ids:
            cells = [t for t in (cell_text(c) for r in grid for c in r) if t and t not in ("$", "-")][:3]
            summary = "Continuation of the table above" + (": " + " – ".join(cells) + "." if cells else ".")
            tree.set_attr(table, "/Table", "/Summary", String(summary))
            report.review("tables", f"Added summary (split-table piece): {summary}", page=page, elem=table,
                          proposal={"field": "summary", "old": "", "value": summary, "applied": True})
        elif cfg["add_summary"] and tree.get_attr(table, "/Table", "/Summary") is None:
            # Screen readers already announce "table with N rows and M columns", so a
            # summary repeating that is noise. It should say what the table is for.
            header_rows = [r for r in grid if r and all(tree.std_type(c) == "TH" for c in r)][:3]
            headers = [[cell_text(c) for c in r] for r in header_rows]
            sample = [[cell_text(c) for c in r] for r in grid if r not in header_rows][:2]
            summary = _ai_summary(ctx, n, headers, sample, len(rows), max(widths))
            source = "Claude"
            if not summary:
                flat = [h for r in headers for h in r if h][:10]
                summary = ("Column headers: " + ", ".join(flat) + ".") if flat else ""
                source = "column headers"
            if not summary:  # no header cells: describe it by its first row (Acrobat needs a summary)
                first = [t for t in (cell_text(c) for c in (grid[0] if grid else [])) if t][:6]
                summary = ("Table beginning: " + ", ".join(first) + ".") if first else "Data table."
                source = "first row"
            tree.set_attr(table, "/Table", "/Summary", String(summary))
            report.review("tables", f"Added summary ({source}): {summary}", page=page, elem=table,
                          proposal={"field": "summary", "old": "", "value": summary, "applied": True})
