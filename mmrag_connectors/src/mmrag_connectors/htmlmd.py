"""Small HTML -> markdown helpers (no pandas needed)."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag


def table_to_md(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True).replace("|", "\\|") for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def html_to_md(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    root = soup.find("article") or soup.find("main") or soup.body or soup
    parts: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "pre"]):
        if el.find_parent("table") or (el.name in ("ul", "ol") and el.find_parent(["ul", "ol"])):
            continue
        if el.name[0] == "h" and len(el.name) == 2:
            t = el.get_text(" ", strip=True)
            if t:
                parts.append(f"\n{'#' * int(el.name[1])} {t}\n")
        elif el.name == "p":
            t = el.get_text(" ", strip=True)
            if t and not el.find_parent("li"):
                parts.append(f"\n{t}\n")
        elif el.name == "pre":
            parts.append("\n```\n" + el.get_text() + "\n```\n")
        elif el.name in ("ul", "ol"):
            for i, li in enumerate(el.find_all("li", recursive=False), 1):
                parts.append(f"{i}. " if el.name == "ol" else "- ")
                parts[-1] += li.get_text(" ", strip=True)
            parts.append("")
        elif el.name == "table":
            md = table_to_md(el)
            if md:
                parts.append("\n" + md + "\n")
    text = "\n".join(parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
