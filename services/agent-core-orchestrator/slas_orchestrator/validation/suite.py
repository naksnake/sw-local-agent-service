"""INGEST for the Validation Agent: a suite as `.md` or `.xlsx` → normalised items (§10.2).

Markdown: a table with columns Step · Action · Parameters · Cycles · Approved (order free,
matched by header name) or a bullet list `- DC cycle x25, settle 60 s`. Excel: the same
columns in the first sheet. The `.xlsx` reader is standard library only (zip + XML): no
macros are ever looked at, and expat resolves no external entities.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

MAX_ITEMS = 200
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
_CYCLES = re.compile(r"(?:x|×)\s*(\d+)|(\d+)\s*(?:cycles?|times)", re.IGNORECASE)
_SETTLE = re.compile(r"settle\s*(\d+)\s*s", re.IGNORECASE)
_APPROVED = re.compile(r"\bapproved\b", re.IGNORECASE)
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class SuiteError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class SuiteItem(SlasModel):
    n: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1)
    params: dict[str, str] = Field(default_factory=dict)
    cycles: int = Field(default=1, ge=1)
    #: The suite author flagged the destructive step as approved for this run (§10.2).
    approved: bool = False

    def sentence(self) -> str:
        extra = f" ×{self.cycles}" if self.cycles > 1 else ""
        params = ", ".join(f"{k} {v}" for k, v in self.params.items())
        return f"{self.n}. {self.title}{extra}" + (f" ({params})" if params else "")


class Suite(SlasModel):
    title: str = Field(min_length=1)
    source: str
    items: list[SuiteItem] = Field(min_length=1, max_length=MAX_ITEMS)

    def sentence(self) -> str:
        cycles = sum(i.cycles for i in self.items)
        return f"{self.title}: {len(self.items)} items, {cycles} cycles in total."


def _params_from_text(text: str) -> tuple[dict[str, str], int]:
    params: dict[str, str] = {}
    cycles = 1
    match = _CYCLES.search(text)
    if match:
        cycles = int(match.group(1) or match.group(2))
    settle = _SETTLE.search(text)
    if settle:
        params["settle_s"] = settle.group(1)
    return params, cycles


def _clean_title(text: str) -> str:
    text = _CYCLES.sub("", text)
    text = _SETTLE.sub("", text)
    text = re.sub(r"\(\s*[,;]?\s*\)", "", text)
    return re.sub(r"\s+", " ", text).strip(" ,;:-") or text.strip()


def _rows_from_markdown_table(lines: list[str]) -> list[list[str]]:
    rows = [line.strip().strip("|").split("|") for line in lines if line.strip().startswith("|")]
    rows = [[cell.strip() for cell in row] for row in rows]
    return [row for row in rows if row and not all(set(cell) <= set("-: ") for cell in row)]


def items_from_rows(rows: list[list[str]], *, source: str) -> list[SuiteItem]:
    if not rows:
        return []
    header = [cell.strip().lower() for cell in rows[0]]

    def column(*names: str) -> int | None:
        for name in names:
            if name in header:
                return header.index(name)
        return None

    col_step = column("step", "title", "test", "item", "name")
    col_action = column("action", "primitive", "do")
    col_params = column("parameters", "params", "args", "settings")
    col_cycles = column("cycles", "repeat", "count")
    col_approved = column("approved", "destructive approved", "approval", "ok to run")
    if col_step is None and col_action is None:
        raise SuiteError(
            ThreePartMessage(
                f"{source} has a table without a Step or Action column.",
                "The suite table needs at least a Step column; Action, Parameters, Cycles and "
                "Approved are optional.",
                "Add the header row and try again.",
            )
        )
    items: list[SuiteItem] = []
    for row in rows[1:]:
        cells = [*row, *([""] * (len(header) - len(row)))]
        title = cells[col_step] if col_step is not None else cells[col_action or 0]
        action = cells[col_action] if col_action is not None else title
        if not title.strip() and not action.strip():
            continue
        params_text = cells[col_params] if col_params is not None else ""
        params, cycles_in_text = _params_from_text(" ".join([title, action, params_text]))
        cycles = cycles_in_text
        if col_cycles is not None and cells[col_cycles].strip():
            try:
                cycles = int(float(cells[col_cycles]))
            except ValueError:
                cycles = cycles_in_text
        for pair in re.split(r"[;,]\s*", params_text):
            key, sep, value = pair.partition("=")
            if sep and key.strip():
                params[key.strip().lower().replace(" ", "_")] = value.strip()
        approved = bool(
            col_approved is not None
            and cells[col_approved].strip().lower() in ("yes", "y", "true", "approved", "x", "✓")
        ) or bool(_APPROVED.search(params_text))
        items.append(
            SuiteItem(
                n=len(items) + 1,
                title=_clean_title(title)[:200] or action.strip()[:200],
                action=(action or title).strip(),
                params=params,
                cycles=max(1, min(cycles, 10_000)),
                approved=approved,
            )
        )
    return items


def parse_suite_md(text: str, *, source: str = "suite.md") -> Suite:
    if not text.strip():
        raise SuiteError(
            ThreePartMessage(
                f"{source} is empty.",
                "A suite needs a title and at least one item.",
                "Add the items and upload again.",
            )
        )
    lines = text.splitlines()
    heading = next(
        (line[2:].strip() for line in lines if line.startswith("# ")),
        Path(source).stem.replace("-", " "),
    )
    table_lines = [line for line in lines if line.strip().startswith("|")]
    items = (
        items_from_rows(_rows_from_markdown_table(table_lines), source=source)
        if table_lines
        else []
    )
    if not items:
        for line in lines:
            match = _BULLET.match(line)
            if not match:
                continue
            raw = match.group(1)
            params, cycles = _params_from_text(raw)
            items.append(
                SuiteItem(
                    n=len(items) + 1,
                    title=_clean_title(_APPROVED.sub("", raw))[:200],
                    action=_clean_title(_APPROVED.sub("", raw)),
                    params=params,
                    cycles=cycles,
                    approved=bool(_APPROVED.search(raw)),
                )
            )
    if not items:
        raise SuiteError(
            ThreePartMessage(
                f"{source} has no items.",
                "Items are table rows under a Step/Action header, or bullets such as "
                "`- DC cycle x25, settle 60 s`.",
                "Add the items and upload again.",
            )
        )
    return Suite(title=heading, source=source, items=items[:MAX_ITEMS])


def read_xlsx_rows(path: Path) -> list[list[str]]:
    """The first worksheet as rows of strings. Standard library only; macros are never read."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))  # noqa: S314 — expat, no external entities
                for si in root.findall("m:si", _NS):
                    shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
            sheet_name = next(
                (n for n in sorted(names) if n.startswith("xl/worksheets/sheet")), None
            )
            if sheet_name is None:
                raise SuiteError(
                    ThreePartMessage(
                        f"{path.name} has no worksheet.",
                        "The workbook is empty or not an .xlsx file.",
                        "Save the suite as .xlsx with the items on the first sheet.",
                    )
                )
            root = ElementTree.fromstring(archive.read(sheet_name))  # noqa: S314
    except zipfile.BadZipFile as exc:
        raise SuiteError(
            ThreePartMessage(
                f"{path.name} is not an .xlsx file.",
                str(exc),
                "Save the suite as .xlsx and upload it again.",
            )
        ) from exc
    except ElementTree.ParseError as exc:
        raise SuiteError(
            ThreePartMessage(
                f"{path.name} could not be read.",
                f"Its XML is damaged ({exc}).",
                "Open and re-save the workbook, then upload it again.",
            )
        ) from exc
    rows: list[list[str]] = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        cells: list[str] = []
        for cell in row.findall("m:c", _NS):
            # Empty cells are omitted from the file; the `r` attribute (e.g. "C4") places
            # each value in its real column so the header lines up with the rows.
            column = _column_index(cell.get("r") or "")
            while column is not None and len(cells) < column:
                cells.append("")
            kind = cell.get("t")
            value_el = cell.find("m:v", _NS)
            inline = cell.find("m:is", _NS)
            if kind == "s" and value_el is not None and value_el.text is not None:
                index = int(value_el.text)
                cells.append(shared[index] if index < len(shared) else "")
            elif kind == "inlineStr" and inline is not None:
                cells.append("".join(t.text or "" for t in inline.iter(f"{{{_NS['m']}}}t")))
            elif value_el is not None and value_el.text is not None:
                cells.append(value_el.text)
            else:
                cells.append("")
        if any(c.strip() for c in cells):
            rows.append(cells)
    return rows


def _column_index(ref: str) -> int | None:
    """ "C4" → 2; None when the reference carries no column letters."""
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    if not letters:
        return None
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def parse_suite_xlsx(path: Path) -> Suite:
    rows = read_xlsx_rows(path)
    items = items_from_rows(rows, source=path.name)
    if not items:
        raise SuiteError(
            ThreePartMessage(
                f"{path.name} has no items.",
                "The first sheet needs a header row (Step, Action, Parameters, Cycles, "
                "Approved) and one row per item.",
                "Add the rows and upload again.",
            )
        )
    return Suite(title=path.stem.replace("-", " ").replace("_", " "), source=path.name, items=items)
