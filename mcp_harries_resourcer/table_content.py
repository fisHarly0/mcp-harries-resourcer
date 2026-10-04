"""Normalize HTML row groups and make selected XML tables valid GFM tables."""
import re
from uuid import uuid4


def _span(value, remaining):
    digits = re.fullmatch(r"\+?([0-9]+)", str(value).strip())
    if not digits:
        return 1
    value = digits[1].lstrip("0") or "0"
    # Avoid parsing an arbitrarily long integer supplied in an HTML attribute.
    count = int(value) if len(value) <= 6 else remaining
    return remaining if count == 0 else min(count, remaining)


def prepare_tables(soup):
    """Resolve spans while row-group boundaries still exist; keep source cells."""
    relocated = {}
    anchors = {}
    for table in soup.find_all("table"):
        groups = []
        direct_rows = []
        for child in list(table.children):
            if child.name == "tr":
                direct_rows.append(child)
            elif child.name in {"thead", "tbody", "tfoot"}:
                if direct_rows:
                    groups.append(direct_rows)
                    direct_rows = []
                groups.append(child.find_all("tr", recursive=False))
        if direct_rows:
            groups.append(direct_rows)
        for rows in groups:
            for index, row in enumerate(rows):
                for cell in row.find_all(["td", "th"], recursive=False):
                    if cell.has_attr("rowspan"):
                        cell["rowspan"] = str(_span(cell["rowspan"], len(rows) - index))
                    # HTML colspan=0 means one column, not zero occupied slots.
                    if cell.has_attr("colspan") and re.fullmatch(r"\+?0+", str(cell["colspan"]).strip()):
                        cell["colspan"] = "1"
        # A caption describes the whole table, not a second header row. Moving
        # it within the same article preserves inline formatting and references.
        for caption in list(table.find_all("caption", recursive=False)):
            caption.extract()
            caption.name = "p"
            table.insert_before(caption)
        # The article extractor flattens tables inside list/definition items.
        # Extract those tables next to their enclosing list, then restore them
        # at the retained location in the selected XML tree.
        lists = table.find_parents(["ol", "ul", "dl"])
        first_cell = table.find(["td", "th"])
        if lists and first_cell is not None and table.find_parent("table") is None:
            marker = "RESOURCERTABLE" + uuid4().hex
            placeholder = soup.new_tag("span")
            placeholder.string = marker + "LOCATION"
            first_cell.insert(0, marker + "DATA")
            table.insert_before(placeholder)
            outer = lists[-1]
            anchor = anchors.get(id(outer), outer)
            anchor.insert_after(table.extract())
            anchors[id(outer)] = table
            relocated[marker + "LOCATION"] = marker + "DATA"
    return relocated


def restore_nested_tables(body, relocated):
    for location, data in relocated.items():
        selected = next((table for table in body.iter("table") if data in "".join(table.itertext())), None)
        if selected is not None:
            found = False
            for node in list(body.iter()):
                for attr in ("text", "tail"):
                    value = getattr(node, attr) or ""
                    if location not in value:
                        continue
                    before, after = value.split(location, 1)
                    source_parent = selected.getparent()
                    if selected.tail:
                        previous = selected.getprevious()
                        if previous is not None:
                            previous.tail = (previous.tail or "") + selected.tail
                        else:
                            source_parent.text = (source_parent.text or "") + selected.tail
                    source_parent.remove(selected)
                    setattr(node, attr, before or None)
                    if attr == "text":
                        node.insert(0, selected)
                    else:
                        parent = node.getparent()
                        parent.insert(parent.index(node) + 1, selected)
                    selected.tail = after or None
                    found = True
                    break
                if found:
                    break
        # Selection may retain only one side; never expose internal markers.
        for node in body.iter():
            for attr in ("text", "tail"):
                value = getattr(node, attr)
                if value:
                    setattr(node, attr, value.replace(location, "").replace(data, ""))


def normalize_selected_tables(body):
    """Use one header delimiter and equal widths without promoting data to headers."""
    for table in body.iter("table"):
        rows = table.findall("row")
        if not rows:
            continue
        width = max(len(row.findall("cell")) for row in rows)
        if not width:
            continue
        if not any(cell.get("role") == "head" for cell in rows[0].findall("cell")):
            header = table.makeelement("row", {})
            for _ in range(width):
                header.append(table.makeelement("cell", {"role": "head"}))
            table.insert(0, header)
        else:
            header = rows[0]
        for row in table.findall("row"):
            cells = row.findall("cell")
            for _ in range(width - len(cells)):
                cell = table.makeelement("cell", {})
                row.append(cell)
                cells.append(cell)
            for cell in cells:
                if row is header:
                    cell.set("role", "head")
                else:
                    cell.attrib.pop("role", None)
