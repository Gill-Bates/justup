#!/usr/bin/env python3
#
# tests/_modal_html.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from html.parser import HTMLParser


def divs_with_class(source: str, token: str) -> list[tuple[int, str, frozenset[str]]]:
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    class Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.tags: list[tuple[int, str, frozenset[str]]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() != "div":
                return
            classes = frozenset(dict(attrs).get("class", "").split())
            if token not in classes:
                return
            line, column = self.getpos()
            self.tags.append((offsets[line - 1] + column, self.get_starttag_text(), classes))

    parser = Collector()
    parser.feed(source)
    parser.close()
    return parser.tags
