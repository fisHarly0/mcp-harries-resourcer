"""Bounded match previews and deterministic selection of local files."""
from dataclasses import dataclass, field
import heapq


def match_preview(line, folded, position, needle, expired):
    """Map a casefold hit to its original character column and show its context."""
    offset = position
    if len(folded) != len(line):
        # Unicode casefold can expand characters (ß -> ss). Skip blocks before
        # mapping only the block containing the first matched character.
        remaining = position
        for start in range(0, len(line), 4096):
            if expired():
                return None
            block = line[start:start + 4096]
            width = len(block.casefold())
            if remaining >= width:
                remaining -= width
                continue
            for index, character in enumerate(block):
                width = len(character.casefold())
                if remaining < width:
                    offset = start + index
                    break
                remaining -= width
            break
    start = max(0, offset - (60 if len(needle) <= 140 else 0))
    end = min(len(line), start + 200)
    return {"text": line[start:end], "column": offset + 1,
            "text_start": start, "text_end": end, "source_chars": len(line),
            "text_truncated": start > 0 or end < len(line)}


@dataclass
class _Candidate:
    order: tuple
    hit: dict = field(compare=False)

    def __lt__(self, other):
        # The least desirable retained file is at the root of the min-heap.
        return self.order > other.order


class RankedFiles:
    def __init__(self, limit):
        self.limit = limit
        self.count = 0
        self.heap = []

    def add(self, hit, priority, relative_path):
        self.count += 1
        candidate = _Candidate((-priority, relative_path.casefold(), relative_path), hit)
        if len(self.heap) < self.limit:
            heapq.heappush(self.heap, candidate)
        elif candidate.order < self.heap[0].order:
            heapq.heapreplace(self.heap, candidate)

    def results(self):
        return [candidate.hit for candidate in sorted(self.heap, key=lambda entry: entry.order)]
