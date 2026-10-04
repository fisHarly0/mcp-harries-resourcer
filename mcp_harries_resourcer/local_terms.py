"""Bounded literal-term AND/OR matching, without tokenizers or query operators."""
from .local_results import match_preview


class TermQuery:
    def __init__(self, query, mode):
        if len(query) > 4096:
            raise ValueError("多关键词查询最多 4096 个字符")
        # Keep the first spelling for diagnostics; casefold-equivalent terms
        # must not become independent requirements or duplicate evidence.
        unique = {}
        for term in query.split():
            unique.setdefault(term.casefold(), term)
        if len(unique) > 16:
            raise ValueError("多关键词查询最多 16 个不同关键词")
        self.needles = tuple(unique)
        self.terms = tuple(unique.values())
        self.mode = mode

    def positions(self, value, expired):
        folded = value.casefold()
        found = {}
        for index, needle in enumerate(self.needles):
            if expired():
                return None
            position = folded.find(needle)
            if position >= 0:
                found[index] = position
        return folded, found

    def accepts(self, found):
        return bool(found) and (self.mode == "any" or len(found) == len(self.terms))

    def evidence(self, value, folded, index, position, source, line, expired):
        window = match_preview(value, folded, position, self.needles[index], expired)
        if window is None or expired():
            return None
        return {"term": self.terms[index], "preview_source": source, "line": line, **window}

    def attach(self, base, primary, evidence):
        return {**base, **{k: v for k, v in primary.items() if k != "term"},
                "matched_terms": [term for i, term in enumerate(self.terms) if i in evidence],
                "term_matches": [evidence[i] for i in range(len(self.terms)) if i in evidence]}

    def lines(self, lines, base, expired):
        for lineno, line in enumerate(lines, 1):
            found = self.positions(line, expired)
            if found is None:
                return
            folded, positions = found
            if not self.accepts(positions):
                continue
            evidence = {}
            for index, position in positions.items():
                hit = self.evidence(line, folded, index, position, "line", lineno, expired)
                if hit is None:
                    return
                evidence[index] = hit
            primary = evidence[min(positions, key=positions.get)]
            yield self.attach(base, primary, evidence)

    def file(self, lines, meta, filename, base, expired):
        best, evidence, fields = {}, {}, set()
        first_line = None
        first_field = None
        matched_lines = 0

        def inspect(value, field, priority, lineno=None):
            nonlocal first_line, first_field, matched_lines
            found = self.positions(value, expired)
            if found is None:
                return False
            folded, positions = found
            if not positions:
                return True
            fields.add(field)
            if field == "text":
                matched_lines += 1
            first = min(positions, key=positions.get)
            for index, position in positions.items():
                best[index] = max(best.get(index, 0), priority)
                needs_line = field == "text" and (index not in evidence or evidence[index]["line"] is None)
                if index not in evidence or needs_line:
                    hit = self.evidence(value, folded, index, position,
                                        "line" if field == "text" else field, lineno, expired)
                    if hit is None:
                        return False
                    evidence[index] = hit
                if index == first:
                    if field == "text" and first_line is None:
                        first_line = evidence[index]
                    elif field != "text" and first_field is None:
                        first_field = evidence[index]
            return True

        if not inspect(meta.get("title", ""), "title", 4):
            return None
        for tag in meta.get("tags", []):
            if not inspect(tag, "tags", 3):
                return None
        if not inspect(filename, "filename", 2):
            return None
        for lineno, line in enumerate(lines, 1):
            if not inspect(line, "text", 1, lineno):
                return None
        # No candidate from a partly checked file, including metadata-only hits.
        if expired() or not self.accepts(best):
            return None
        hit = self.attach(base, first_line or first_field, evidence)
        hit.update(matched_fields=[f for f in ("title", "tags", "filename", "text") if f in fields],
                   matched_lines=matched_lines)
        # AND must cover every term at this tier: one title term must not raise
        # a file above another whose entire query matches its title or tags.
        priority = min(best.values()) if self.mode == "all" else max(best.values())
        hit["rank_field"] = {4: "title", 3: "tags", 2: "filename", 1: "text"}[priority]
        return hit, priority
