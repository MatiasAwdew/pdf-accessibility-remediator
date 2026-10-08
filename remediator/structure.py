"""Helpers for reading and editing a PDF's logical structure tree.

The structure tree is the part of a tagged PDF that screen readers use:
StructTreeRoot -> StructElem (/S /H1, /P, /Figure, ...) -> kids, where a kid is
another StructElem, a marked-content id (an int MCID, or an /MCR dict), or an
object reference (/OBJR, used for annotations such as links).

The ParentTree is the reverse index: page /StructParents -> array indexed by
MCID -> owning StructElem, and annotation /StructParent -> owning StructElem.
Any edit that moves an MCID or OBJR to a different element must update it, or
PAC / Acrobat will report a broken tree. Every mutation here goes through
helpers that keep both directions in sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

import pikepdf
from pikepdf import Array, Dictionary, Name

# Standard structure types of PDF 1.7 (ISO 32000-1), which PDF/UA-1 and PAC 3 check
# against. Anything else must be role-mapped to one of these.
STANDARD_TYPES = {
    "Document", "Part", "Art", "Sect", "Div", "BlockQuote", "Caption", "TOC", "TOCI",
    "Index", "NonStruct", "Private", "P", "H", "H1", "H2", "H3", "H4", "H5", "H6",
    "L", "LI", "Lbl", "LBody", "Table", "TR", "TH", "TD", "THead", "TBody", "TFoot",
    "Span", "Quote", "Note", "Reference", "BibEntry", "Code", "Link", "Annot", "Ruby",
    "RB", "RT", "RP", "Warichu", "WT", "WP", "Figure", "Formula", "Form",
}
# PDF 2.0 / Word / Adobe AutoTag types and their nearest PDF 1.7 equivalent
NEAREST_STANDARD = {
    "Title": "P", "Aside": "Div", "FENote": "Note", "Footnote": "Note", "Endnote": "Note",
    "Sub": "Span", "Em": "Span", "Strong": "Span", "StyleSpan": "Span", "DocumentFragment": "Div",
    "Textbox": "Div", "TextBox": "Div", "Chart": "Figure", "Diagram": "Figure", "Image": "Figure",
    "Heading": "H", "Bibliography": "BibEntry", "InlineShape": "Figure", "Shape": "Figure",
}
HEADING_TYPES = {"H1", "H2", "H3", "H4", "H5", "H6"}


def as_list(obj) -> list:
    if obj is None:
        return []
    if isinstance(obj, Array):
        return list(obj)
    return [obj]


def is_struct_elem(obj) -> bool:
    return isinstance(obj, Dictionary) and "/S" in obj


def is_mcr(obj) -> bool:
    return isinstance(obj, Dictionary) and obj.get("/Type") == Name.MCR


def is_objr(obj) -> bool:
    return isinstance(obj, Dictionary) and obj.get("/Type") == Name.OBJR


def pdf_str(obj) -> str:
    if obj is None:
        return ""
    try:
        return str(obj)
    except Exception:
        return ""


def _read_num_tree(node, out: dict, depth: int = 0) -> None:
    if depth > 32 or not isinstance(node, Dictionary):
        return
    nums = node.get("/Nums")
    if nums is not None:
        for k in range(0, len(nums) - 1, 2):
            out[int(nums[k])] = nums[k + 1]
    for kid in as_list(node.get("/Kids")):
        _read_num_tree(kid, out, depth + 1)


@dataclass
class Node:
    elem: Dictionary
    parent: "Node | None"
    depth: int
    path: str
    children: list["Node"] = field(default_factory=list)

    @property
    def raw_type(self) -> str:
        return str(self.elem.get("/S"))[1:]


class StructTree:
    def __init__(self, pdf: pikepdf.Pdf):
        self.pdf = pdf
        self.root = pdf.Root.get("/StructTreeRoot")
        self.page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
        self.role_map = {}
        if self.root is not None and "/RoleMap" in self.root:
            for k, v in self.root.RoleMap.items():
                self.role_map[k[1:]] = str(v)[1:]
        self._parent_tree = None

    @property
    def tagged(self) -> bool:
        return self.root is not None

    # ---------- traversal ----------

    def top_level(self) -> list[Dictionary]:
        if self.root is None:
            return []
        return [k for k in as_list(self.root.get("/K")) if is_struct_elem(k)]

    def build(self) -> list[Node]:
        """Return all nodes in document (depth-first) order, with parent links."""
        out: list[Node] = []
        seen: set = set()

        def visit(elem, parent, depth, path):
            key = elem.objgen if elem.is_indirect else id(elem)
            if key in seen:  # guard against cyclic/malformed trees
                return
            seen.add(key)
            node = Node(elem, parent, depth, path)
            if parent:
                parent.children.append(node)
            out.append(node)
            i = 0
            for kid in as_list(elem.get("/K")):
                if is_struct_elem(kid):
                    visit(kid, node, depth + 1, f"{path}/{i}")
                    i += 1

        for i, elem in enumerate(self.top_level()):
            visit(elem, None, 0, str(i))
        return out

    def find_by_path(self, path: str) -> Dictionary | None:
        for n in self.build():
            if n.path == path:
                return n.elem
        return None

    def std_type(self, elem: Dictionary) -> str:
        t = str(elem.get("/S"))[1:]
        seen = set()
        while t not in STANDARD_TYPES and t in self.role_map and t not in seen:
            seen.add(t)
            t = self.role_map[t]
        return t

    def page_of(self, elem: Dictionary) -> int | None:
        """Page of elem: its own/inherited /Pg, else the page of its first content."""
        page = self._page_up(elem)
        if page is None:
            for kid in as_list(elem.get("/K")):
                if is_struct_elem(kid):
                    page = self.page_of(kid)
                elif is_mcr(kid) or is_objr(kid):
                    pg = kid.get("/Pg")
                    page = self.page_index.get(pg.objgen) if pg is not None else None
                if page is not None:
                    break
        return page

    def _page_up(self, elem: Dictionary) -> int | None:
        cur = elem
        for _ in range(64):
            if cur is None or not isinstance(cur, Dictionary):
                return None
            pg = cur.get("/Pg")
            if pg is not None:
                return self.page_index.get(pg.objgen)
            cur = cur.get("/P")
        return None

    def direct_mcids(self, elem: Dictionary) -> list[tuple[int | None, int]]:
        """(page_index, mcid) for marked content directly owned by elem."""
        out = []
        own_page = self.page_of(elem)
        for kid in as_list(elem.get("/K")):
            if isinstance(kid, int):
                out.append((own_page, int(kid)))
            elif is_mcr(kid) and "/MCID" in kid:
                pg = kid.get("/Pg")
                page = self.page_index.get(pg.objgen) if pg is not None else own_page
                out.append((page, int(kid.MCID)))
        return out

    def all_mcids(self, elem: Dictionary) -> list[tuple[int | None, int]]:
        out = self.direct_mcids(elem)
        for kid in as_list(elem.get("/K")):
            if is_struct_elem(kid):
                out.extend(self.all_mcids(kid))
        return out

    def objrs(self, elem: Dictionary) -> list[Dictionary]:
        return [k for k in as_list(elem.get("/K")) if is_objr(k)]

    # ---------- parent tree ----------

    @property
    def parent_tree(self) -> pikepdf.NumberTree:
        if self._parent_tree is None:
            if "/ParentTree" not in self.root:
                self.root.ParentTree = self.pdf.make_indirect(Dictionary(Nums=Array()))
            elif "/Kids" in self.root.ParentTree:
                # Multi-level trees (Adobe AutoTag writes them) can't be updated
                # reliably in place; flatten to one /Nums array first.
                entries: dict = {}
                _read_num_tree(self.root.ParentTree, entries)
                flat = Array()
                for k in sorted(entries):
                    flat.append(k)
                    flat.append(entries[k])
                self.root.ParentTree = self.pdf.make_indirect(Dictionary(Nums=flat))
            self._parent_tree = pikepdf.NumberTree(self.root.ParentTree)
        return self._parent_tree

    def next_parent_key(self) -> int:
        # Also avoid keys pages/annotations already claim: untagged PDFs from some
        # producers keep stale /StructParents numbers with no ParentTree entry.
        used = [int(k) + 1 for k, _ in self.parent_tree.items()]
        for page in self.pdf.pages:
            if "/StructParents" in page.obj:
                used.append(int(page.obj.StructParents) + 1)
            for a in page.obj.get("/Annots") or []:
                if isinstance(a, Dictionary) and "/StructParent" in a:
                    used.append(int(a.StructParent) + 1)
        key = max([int(self.root.get("/ParentTreeNextKey", 0))] + used)
        self.root.ParentTreeNextKey = key + 1
        return key

    def set_mcid_owner(self, page_idx: int, mcid: int, elem: Dictionary) -> None:
        page = self.pdf.pages[page_idx].obj
        if "/StructParents" not in page:
            key = self.next_parent_key()
            page.StructParents = key
            self.parent_tree[key] = self.pdf.make_indirect(Array())
        try:
            arr = self.parent_tree[int(page.StructParents)]
        except (KeyError, IndexError):  # stale /StructParents with no entry: create it
            arr = self.pdf.make_indirect(Array())
            self.parent_tree[int(page.StructParents)] = arr
        if not isinstance(arr, Array):
            arr = self.pdf.make_indirect(Array())
            self.parent_tree[int(page.StructParents)] = arr
        while len(arr) <= mcid:
            arr.append(pikepdf.Object.parse(b"null"))
        arr[mcid] = elem

    def set_objr_owner(self, annot: Dictionary, elem: Dictionary) -> None:
        if "/StructParent" in annot:
            self.parent_tree[int(annot.StructParent)] = elem
        else:
            key = self.next_parent_key()
            annot.StructParent = key
            self.parent_tree[key] = elem

    # ---------- mutation ----------

    def new_elem(self, s_type: str, parent: Dictionary, page_idx: int | None = None) -> Dictionary:
        d = Dictionary(Type=Name.StructElem, S=Name("/" + s_type), P=parent, K=Array())
        if page_idx is not None:
            d.Pg = self.pdf.pages[page_idx].obj
        return self.pdf.make_indirect(d)

    def set_kids(self, elem: Dictionary, kids: list) -> None:
        elem.K = Array(kids)

    def reparent_kid(self, kid, new_parent: Dictionary, old_page: int | None) -> object:
        """Point a kid (elem / mcid / MCR / OBJR) at new_parent, fixing back-refs.

        Returns the kid object to store in new_parent's /K. Bare-int MCIDs are
        converted to explicit MCRs when the new parent is on a different page.
        """
        if is_struct_elem(kid):
            kid.P = new_parent
            return kid
        new_page = self.page_of(new_parent)
        if isinstance(kid, int):
            if old_page is not None:
                self.set_mcid_owner(old_page, int(kid), new_parent)
            if old_page is not None and old_page != new_page:
                return Dictionary(Type=Name.MCR, MCID=int(kid), Pg=self.pdf.pages[old_page].obj)
            return kid
        if is_mcr(kid):
            pg = kid.get("/Pg")
            page = self.page_index.get(pg.objgen) if pg is not None else old_page
            if page is not None and "/MCID" in kid:
                self.set_mcid_owner(page, int(kid.MCID), new_parent)
            if pg is None and page is not None:
                kid.Pg = self.pdf.pages[page].obj
            return kid
        if is_objr(kid):
            self.set_objr_owner(kid.Obj, new_parent)
            return kid
        return kid

    def wrap_kids(self, parent: Dictionary, indices: list[int], s_type: str) -> Dictionary:
        """Move parent.K[indices] into a new s_type element inserted at the first index."""
        kids = as_list(parent.get("/K"))
        old_page = self.page_of(parent)
        wrapper = self.new_elem(s_type, parent, old_page)
        moved = [self.reparent_kid(kids[i], wrapper, old_page) for i in indices]
        self.set_kids(wrapper, moved)
        chosen = set(indices)
        rest = [k for i, k in enumerate(kids) if i not in chosen]
        rest.insert(min(indices), wrapper)
        self.set_kids(parent, rest)
        return wrapper

    def remove_elem(self, elem: Dictionary) -> None:
        parent = elem.get("/P")
        container = parent if parent is not None and "/K" in parent else self.root
        kids = [k for k in as_list(container.get("/K"))
                if not (isinstance(k, Dictionary) and k.is_indirect and elem.is_indirect
                        and k.objgen == elem.objgen)]
        container.K = Array(kids)

    def set_type(self, elem: Dictionary, s_type: str) -> None:
        elem.S = Name("/" + s_type)

    # ---------- attributes ----------

    def get_attr(self, elem: Dictionary, owner: str, key: str):
        for a in as_list(elem.get("/A")):
            if isinstance(a, Dictionary) and a.get("/O") == Name(owner) and key in a:
                return a[key]
        return None

    def set_attr(self, elem: Dictionary, owner: str, key: str, value) -> None:
        attrs = as_list(elem.get("/A"))
        for a in attrs:
            if isinstance(a, Dictionary) and a.get("/O") == Name(owner):
                a[key] = value
                return
        new = Dictionary(O=Name(owner))
        new[key] = value
        attrs.append(new)
        elem.A = attrs[0] if len(attrs) == 1 else Array(attrs)
