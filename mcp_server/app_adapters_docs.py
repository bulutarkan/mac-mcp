"""Reading documents (#131): PDFs and images for Preview, tables in Numbers, slides in Keynote, text in Pages.

Nothing here edits a document. Preview's reader opens the file itself with PDFKit (scanned pages through
Vision OCR), so it reads a PDF whether or not Preview shows it. Numbers, Keynote and Pages are read through
their scripting model, and only documents already open there: reading never launches an app.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .app_adapter_core import AppAdapterError, _run_js

MAX_CHARS = 60_000
MAX_TABLE_ROWS = 200
MAX_RANGE_CELLS = 5_000
MAX_OCR_PAGES = 5
OCR_MIN_CHARS = 20
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".gif", ".bmp"})
# Separates document, sheet and table in a Numbers table's item_id.
TABLE_SEP = " › "

_IWORK = {"Numbers": "com.apple.iWork.Numbers", "Keynote": "com.apple.iWork.Keynote", "Pages": "com.apple.iWork.Pages"}

# Every iWork script starts here: argv[0] is the bundle id. The app must be installed and running, and
# `pick(sel)` finds an open document by name or file path (the front one when sel is empty).
_IWORK_JS = """
function fail(code, message) { return JSON.stringify({error: code, message: message, retryable: false}); }
function fileOf(d) { try { var f = d.file(); return f ? String(f) : null; } catch (e) { return null; } }
function opened(id) { try { return Application(id); } catch (e) { return null; } }
function pick(app, sel) {
  var n = app.documents.length;
  if (!n) return null;
  if (!sel) return app.documents[0];
  for (var i = 0; i < n; i++) {
    var d = app.documents[i];
    if (d.name() === sel || fileOf(d) === sel) return d;
  }
  return null;
}
function start(argv, name) {
  var app = opened(argv[0]);
  if (!app) return {early: fail('APP_NOT_INSTALLED', name + ' is not installed on this Mac.')};
  if (!app.running()) {
    return {early: fail('APP_NOT_RUNNING', name + ' is not running; open the file with open_document first '
                        + '(reading never launches the app).')};
  }
  return {app: app};
}
function chosen(app, sel, name) {
  var d = pick(app, sel);
  if (d) return {doc: d};
  return {early: fail('DOCUMENT_NOT_OPEN', sel ? 'No open ' + name + ' document is named or saved at ' + sel
                      + '; list_documents shows the open ones, open_document opens a file.'
                      : name + ' has no open document; open_document opens a file.')};
}
"""


def _cursor(value: Optional[str], *, name: str = "cursor") -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    try:
        number = int(text)
    except ValueError:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"{name} must be a whole number (next_cursor).") from None
    if number < 1:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"{name} starts at 1.")
    return number


def _bundle_id(app: str) -> str:
    return _IWORK[app]


def _selector(item_id: Optional[str], path: Optional[str]) -> str:
    """Which open document: a name from list_documents or a file path; empty means the front one."""
    text = str(item_id or "").strip()
    if text:
        return text
    raw = str(path or "").strip()
    return str(Path(raw).expanduser().resolve(strict=False)) if raw else ""


# Preview ----------------------------------------------------------------------------------------

_PREVIEW_FRONT_JS = """
function run(argv) {
  var app = Application('com.apple.Preview');
  if (!app.running()) return JSON.stringify({running: false, path: null});
  var docs = app.documents;
  for (var i = 0; i < docs.length; i++) {
    try { var p = docs[i].path(); if (p) return JSON.stringify({running: true, path: String(p)}); } catch (e) {}
  }
  return JSON.stringify({running: true, path: null});
}
"""


def _preview_read_js() -> str:
    from .knowledge import OCR_JS

    # argv: path, kind (pdf|image), first page, page count, max OCR pages, min characters for a page to count as text.
    return OCR_JS + """
function str(value) { return value && !value.isNil() ? ObjC.unwrap(value) : null; }
// The characters whose centre lies in a markup annotation's box. selectionForRect also takes in
// neighbouring lines whose glyph boxes overlap the box, so the centres decide.
function centres(page, text) {
  var out = [];
  for (var i = 0; i < text.length && i < 20000; i++) {
    var c = page.characterBoundsAtIndex(i);
    out.push([c.origin.x + c.size.width / 2, c.origin.y + c.size.height / 2]);
  }
  return out;
}
function words(text, points, a) {
  try {
    var b = a.bounds, out = '';
    for (var i = 0; i < points.length; i++) {
      var p = points[i];
      if (p[0] >= b.origin.x && p[0] <= b.origin.x + b.size.width && p[1] >= b.origin.y
          && p[1] <= b.origin.y + b.size.height) out += text[i];
    }
    return out.replace(/\\s+/g, ' ').trim() || null;
  } catch (e) { return null; }
}
function run(argv) {
  var path = argv[0], kind = argv[1], first = Number(argv[2]), count = Number(argv[3]);
  var maxOcr = Number(argv[4]), minChars = Number(argv[5]);
  if (kind === 'image') {
    var picture = $.NSImage.alloc.initWithContentsOfFile(path);
    if (!picture || picture.isNil()) {
      return JSON.stringify({error: 'PREVIEW_DOCUMENT_UNREADABLE', message: 'The image could not be read.'});
    }
    var seen = ocr(cgOf(picture, picture.size.width, picture.size.height));
    if (seen === null) {
      return JSON.stringify({error: 'PREVIEW_OCR_UNAVAILABLE', message: 'Text recognition is not available.'});
    }
    return JSON.stringify({kind: 'image', page_count: 1, pages: [{page: 1, text: seen, ocr: true, notes: []}]});
  }
  var doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(path));
  if (!doc || doc.isNil()) {
    return JSON.stringify({error: 'PREVIEW_DOCUMENT_UNREADABLE', message: 'The PDF could not be read.'});
  }
  if (doc.isLocked) {
    return JSON.stringify({error: 'PREVIEW_DOCUMENT_LOCKED', message: 'The PDF is locked with a password.',
                           retryable: false});
  }
  var attrs = doc.documentAttributes, info = {};
  if (attrs && !attrs.isNil()) {
    ['Title', 'Author', 'Subject'].forEach(function (key) { info[key.toLowerCase()] = str(attrs.objectForKey(key)); });
  }
  var total = Number(doc.pageCount), pages = [], read = 0;
  for (var i = first - 1; i < Math.min(total, first - 1 + count); i++) {
    var page = doc.pageAtIndex(i), text = str(page.string) || '', used = false;
    if (text.replace(/\\s/g, '').length < minChars && read < maxOcr) {
      var box = page.boundsForBox(0), w = box.size.width * 2, h = box.size.height * 2;
      var seen = ocr(cgOf(page.thumbnailOfSizeForBox($.NSMakeSize(w, h), 0), w, h));
      read += 1;
      if (seen && seen.replace(/\\s/g, '').length > text.replace(/\\s/g, '').length) { text = seen; used = true; }
    }
    var notes = [], marks = page.annotations, own = str(page.string) || '', points = null;
    for (var j = 0; marks && j < marks.count; j++) {
      var a = marks.objectAtIndex(j), type = str(a.type) || '';
      if (type === 'Link' || type === 'Widget' || type === 'Popup') continue;
      var note = {type: type, text: str(a.contents)};
      if (['Highlight', 'Underline', 'StrikeOut'].indexOf(type) >= 0) {
        if (points === null) points = centres(page, own);
        note.marked_text = words(own, points, a);
      }
      notes.push(note);
    }
    pages.push({page: i + 1, text: text, ocr: used, notes: notes});
  }
  return JSON.stringify({kind: 'pdf', page_count: total, title: info.title || null, author: info.author || null,
                         subject: info.subject || null, pages: pages, ocr_pages: read});
}
"""


def _in_scope(target: Path) -> None:
    """A restricted agent reads only inside its paths, as with read_file."""
    from .policy import current_policy_context
    from .policy_scope import path_is_within

    scope = current_policy_context().scope
    roots = scope.path_roots if scope is not None else None
    if roots and not any(path_is_within(target, root) for root in roots):
        raise AppAdapterError("SCOPE_PATH_DENIED", "That file is outside the paths this agent may read.",
                              retryable=False)


def preview_read(path: Optional[str], *, cursor: Optional[str], limit: int, timeout_s: float) -> Dict[str, Any]:
    """Text, scanned-page OCR and annotations of a PDF (or an image), a few pages per call."""
    raw = str(path or "").strip()
    if not raw:
        front = _run_js(_PREVIEW_FRONT_JS, [], timeout_s=min(timeout_s, 5.0), code="PREVIEW_LIST_FAILED")
        if not front.get("path"):
            raise AppAdapterError(
                "PREVIEW_PATH_REQUIRED",
                "Pass path = the file to read" + ("" if front.get("running") else " (Preview is not running)")
                + "; without it the front Preview document is read.", retryable=False,
            )
        raw = str(front["path"])
    target = Path(raw).expanduser().resolve(strict=True)
    if not target.is_file():
        raise AppAdapterError("PREVIEW_PATH_INVALID", "read_document needs an existing regular file.")
    suffix = target.suffix.lower()
    if suffix != ".pdf" and suffix not in _IMAGE_SUFFIXES:
        raise AppAdapterError("PREVIEW_UNSUPPORTED_TYPE",
                              "read_document reads PDFs and images; use read_file for text files.", retryable=False)
    _in_scope(target)
    first = _cursor(cursor)
    payload = _run_js(
        _preview_read_js(),
        [str(target), "pdf" if suffix == ".pdf" else "image", str(first), str(limit), str(MAX_OCR_PAGES),
         str(OCR_MIN_CHARS)],
        timeout_s=max(timeout_s, 20.0), code="PREVIEW_READ_FAILED",
    )
    total = int(payload.get("page_count") or 0)
    if total and first > total:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"The document has {total} pages.", retryable=False)
    pages: List[Dict[str, Any]] = []
    used = 0
    for page in payload.get("pages") or []:
        text = str(page.get("text") or "")
        if pages and used + len(text) > MAX_CHARS:
            break
        if len(text) > MAX_CHARS:
            page = {**page, "text": text[:MAX_CHARS], "text_truncated": True}
        used += len(page["text"])
        pages.append(page)
    last = pages[-1]["page"] if pages else first - 1
    result = {
        "ok": True, "path": str(target), "name": target.name, "kind": payload.get("kind"), "page_count": total,
        "pages": pages, "next_cursor": str(last + 1) if last < total else None,
    }
    for key in ("title", "author", "subject"):
        if payload.get(key):
            result[key] = payload[key]
    if any(page.get("ocr") for page in pages):
        result["note"] = "Pages marked ocr=true were scanned; their text was recognised on this Mac and may hold errors."
    return result


# iWork ------------------------------------------------------------------------------------------

_DOCUMENT_PATHS_JS = _IWORK_JS + """
function run(argv) {
  var app = opened(argv[0]);
  if (!app || !app.running()) return JSON.stringify({paths: []});
  var out = [];
  for (var i = 0; i < app.documents.length; i++) { var f = fileOf(app.documents[i]); if (f) out.push(f); }
  return JSON.stringify({paths: out});
}
"""


def document_paths(app: str, *, timeout_s: float = 3.0) -> List[str]:
    """The files open in an iWork app, for open_document's read-back."""
    payload = _run_js(_DOCUMENT_PATHS_JS, [_bundle_id(app)], timeout_s=timeout_s, code=f"{app.upper()}_LIST_FAILED")
    return [str(item) for item in payload.get("paths") or []]


_NUMBERS_LIST_JS = _IWORK_JS + """
function run(argv) {
  var s = start(argv, 'Numbers'); if (s.early) return s.early;
  var app = s.app, max = Number(argv[1]), docs = [];
  for (var i = 0; i < Math.min(app.documents.length, max); i++) {
    var d = app.documents[i], sheets = [], active = null;
    try { active = d.activeSheet.name(); } catch (e) {}
    for (var j = 0; j < d.sheets.length; j++) {
      var sh = d.sheets[j], tables = [];
      for (var k = 0; k < sh.tables.length; k++) {
        var t = sh.tables[k];
        tables.push({name: t.name(), row_count: t.rowCount(), column_count: t.columnCount(),
                     header_rows: t.headerRowCount()});
      }
      sheets.push({name: sh.name(), tables: tables});
    }
    docs.push({name: d.name(), path: fileOf(d), active_sheet: active, sheets: sheets});
  }
  return JSON.stringify({documents: docs, total: app.documents.length});
}
"""

# argv: bundle id, document, sheet, table, range, first row, max rows, max cells.
_NUMBERS_READ_JS = _IWORK_JS + """
function letters(n) {
  var s = '';
  while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}
function run(argv) {
  var s = start(argv, 'Numbers'); if (s.early) return s.early;
  var c = chosen(s.app, argv[1], 'Numbers'); if (c.early) return c.early;
  var d = c.doc, sheetName = argv[2], tableName = argv[3], wanted = argv[4];
  var first = Number(argv[5]), maxRows = Number(argv[6]), maxCells = Number(argv[7]);
  var sh = null;
  if (sheetName) { for (var j = 0; j < d.sheets.length; j++) if (d.sheets[j].name() === sheetName) sh = d.sheets[j]; }
  else { try { sh = d.activeSheet; sh.name(); } catch (e) { sh = d.sheets.length ? d.sheets[0] : null; } }
  if (!sh) return fail('NUMBERS_TABLE_NOT_FOUND', 'No sheet named ' + sheetName + ' in ' + d.name() + '.');
  var t = null;
  if (tableName) { for (var k = 0; k < sh.tables.length; k++) if (sh.tables[k].name() === tableName) t = sh.tables[k]; }
  else if (sh.tables.length) t = sh.tables[0];
  if (!t) return fail('NUMBERS_TABLE_NOT_FOUND', 'No table ' + (tableName ? 'named ' + tableName + ' ' : '')
                      + 'on sheet ' + sh.name() + '.');
  var rows = t.rowCount(), cols = t.columnCount(), headers = t.headerRowCount();
  var name = wanted, last = null;
  if (!name) {
    if (first > rows) return fail('APP_ADAPTER_ARGUMENT_INVALID', 'The table has ' + rows + ' rows.');
    last = Math.min(rows, first + maxRows - 1);
    name = 'A' + first + ':' + letters(cols) + last;
  }
  var r = t.ranges[name], width, height;
  try { width = r.columns.length; height = r.rows.length; }
  catch (e) { return fail('NUMBERS_RANGE_INVALID', 'Not a range of this table: ' + name + ' (use A1:D20).'); }
  if (width * height > maxCells) {
    return fail('NUMBERS_RANGE_TOO_LARGE', name + ' has ' + (width * height) + ' cells; ask for at most '
                + maxCells + ' at a time.');
  }
  var values = r.cells.formattedValue(), out = [];
  for (var y = 0; y < height; y++) out.push(values.slice(y * width, (y + 1) * width));
  var header = null;
  if (headers > 0) { try { header = t.rows[0].cells.formattedValue(); } catch (e) {} }
  var top = first;
  try { top = r.rows[0].address(); } catch (e) {}
  return JSON.stringify({document: d.name(), path: fileOf(d), sheet: sh.name(), table: t.name(), row_count: rows,
                         column_count: cols, header_rows: headers, header: header, range: name, first_row: top,
                         rows: out, window_end: last});
}
"""


def numbers_list(*, limit: int, timeout_s: float) -> Dict[str, Any]:
    payload = _run_js(_NUMBERS_LIST_JS, [_bundle_id("Numbers"), str(limit)], timeout_s=timeout_s,
                      code="NUMBERS_LIST_FAILED")
    documents = payload.get("documents") or []
    for doc in documents:
        for sheet in doc.get("sheets") or []:
            for table in sheet.get("tables") or []:
                table["item_id"] = TABLE_SEP.join((doc["name"], sheet["name"], table["name"]))
    return {"ok": True, "count": len(documents), "documents": documents,
            "truncated": int(payload.get("total") or 0) > len(documents),
            "note": "Read a table with read_table (item_id = a table's item_id, optional query = a range like A1:D20)."}


def _blank(value: Any) -> bool:
    return value is None or value == ""


def numbers_read(item_id: Optional[str], *, path: Optional[str], query: Optional[str], cursor: Optional[str],
                 timeout_s: float) -> Dict[str, Any]:
    """A table's cells as shown in Numbers, a range or 200 rows at a time."""
    text = str(item_id or "").strip()
    parts = text.split(TABLE_SEP) if TABLE_SEP in text else []
    if text and len(parts) != 3:
        # Only a document's name or path: its active sheet's first table.
        parts = [text, "", ""]
    document, sheet, table = parts if parts else [_selector(None, path), "", ""]
    wanted = str(query or "").strip().upper().replace(" ", "")
    first = _cursor(cursor)
    payload = _run_js(
        _NUMBERS_READ_JS,
        [_bundle_id("Numbers"), document, sheet, table, wanted, str(first), str(MAX_TABLE_ROWS), str(MAX_RANGE_CELLS)],
        timeout_s=max(timeout_s, 15.0), code="NUMBERS_READ_FAILED",
    )
    rows: List[List[Any]] = payload.get("rows") or []
    window_end = payload.get("window_end")
    if not wanted:
        # A table starts with empty rows and columns to fill; whole-table reads leave the trailing ones out.
        while rows and all(_blank(value) for value in rows[-1]):
            rows.pop()
        width = max((index + 1 for row in rows for index, value in enumerate(row) if not _blank(value)), default=0)
        rows = [row[:width] for row in rows]
    result = {
        "ok": True, "document": payload.get("document"), "path": payload.get("path"), "sheet": payload.get("sheet"),
        "table": payload.get("table"),
        "item_id": TABLE_SEP.join(str(payload.get(key) or "") for key in ("document", "sheet", "table")),
        "row_count": payload.get("row_count"), "column_count": payload.get("column_count"),
        "header_rows": payload.get("header_rows"), "range": payload.get("range"), "first_row": payload.get("first_row"),
        "rows": rows,
    }
    header = list(payload.get("header") or [])
    while header and _blank(header[-1]):
        header.pop()
    if header and int(payload.get("header_rows") or 0) > 0:
        result["header"] = header
    if window_end is not None:
        result["next_cursor"] = str(window_end + 1) if int(window_end) < int(payload.get("row_count") or 0) else None
    return result


_KEYNOTE_LIST_JS = _IWORK_JS + """
function run(argv) {
  var s = start(argv, 'Keynote'); if (s.early) return s.early;
  var app = s.app, max = Number(argv[1]), docs = [];
  for (var i = 0; i < Math.min(app.documents.length, max); i++) {
    var d = app.documents[i];
    docs.push({name: d.name(), path: fileOf(d), slide_count: d.slides.length});
  }
  return JSON.stringify({documents: docs, total: app.documents.length});
}
"""

# argv: bundle id, document, first slide, slide count.
_KEYNOTE_READ_JS = _IWORK_JS + """
function text(get) { try { var v = get(); return v === null || v === undefined ? null : String(v); } catch (e) { return null; } }
function run(argv) {
  var s = start(argv, 'Keynote'); if (s.early) return s.early;
  var c = chosen(s.app, argv[1], 'Keynote'); if (c.early) return c.early;
  var d = c.doc, first = Number(argv[2]), count = Number(argv[3]), total = d.slides.length, slides = [];
  if (total && first > total) return fail('APP_ADAPTER_ARGUMENT_INVALID', 'The presentation has ' + total + ' slides.');
  for (var i = first - 1; i < Math.min(total, first - 1 + count); i++) {
    var sl = d.slides[i];
    var title = text(function () { return sl.defaultTitleItem.objectText(); });
    var body = text(function () { return sl.defaultBodyItem.objectText(); });
    var other = [];
    try {
      sl.textItems.objectText().forEach(function (t) {
        if (t && t !== title && t !== body) other.push(String(t));
      });
    } catch (e) {}
    var skipped = false;
    try { skipped = !!sl.skipped(); } catch (e) {}
    slides.push({slide: i + 1, title: title || null, body: body || null, other_text: other,
                 presenter_notes: text(function () { return sl.presenterNotes(); }) || null, skipped: skipped});
  }
  return JSON.stringify({document: d.name(), path: fileOf(d), slide_count: total, slides: slides});
}
"""


def keynote_list(*, limit: int, timeout_s: float) -> Dict[str, Any]:
    payload = _run_js(_KEYNOTE_LIST_JS, [_bundle_id("Keynote"), str(limit)], timeout_s=timeout_s,
                      code="KEYNOTE_LIST_FAILED")
    documents = payload.get("documents") or []
    return {"ok": True, "count": len(documents), "documents": documents,
            "truncated": int(payload.get("total") or 0) > len(documents),
            "note": "Read slides with read_slides (item_id = a document's name; cursor/limit page through them)."}


def keynote_read(item_id: Optional[str], *, path: Optional[str], cursor: Optional[str], limit: int,
                 timeout_s: float) -> Dict[str, Any]:
    first = _cursor(cursor)
    payload = _run_js(_KEYNOTE_READ_JS, [_bundle_id("Keynote"), _selector(item_id, path), str(first), str(limit)],
                      timeout_s=max(timeout_s, 15.0), code="KEYNOTE_READ_FAILED")
    slides = payload.get("slides") or []
    total = int(payload.get("slide_count") or 0)
    last = slides[-1]["slide"] if slides else first - 1
    return {"ok": True, "document": payload.get("document"), "path": payload.get("path"), "slide_count": total,
            "slides": slides, "next_cursor": str(last + 1) if last < total else None}


_PAGES_LIST_JS = _IWORK_JS + """
function run(argv) {
  var s = start(argv, 'Pages'); if (s.early) return s.early;
  var app = s.app, max = Number(argv[1]), docs = [];
  for (var i = 0; i < Math.min(app.documents.length, max); i++) {
    var d = app.documents[i], pages = null;
    try { pages = d.pages.length; } catch (e) {}
    docs.push({name: d.name(), path: fileOf(d), page_count: pages});
  }
  return JSON.stringify({documents: docs, total: app.documents.length});
}
"""

_PAGES_READ_JS = _IWORK_JS + """
function run(argv) {
  var s = start(argv, 'Pages'); if (s.early) return s.early;
  var c = chosen(s.app, argv[1], 'Pages'); if (c.early) return c.early;
  var d = c.doc, body = '';
  try { body = String(d.bodyText() || ''); }
  catch (e) { return fail('PAGES_LAYOUT_DOCUMENT', d.name() + ' is a page layout document without body text; '
                          + 'use mac_observe on its window.'); }
  var pages = null;
  try { pages = d.pages.length; } catch (e) {}
  return JSON.stringify({document: d.name(), path: fileOf(d), page_count: pages, text: body});
}
"""


def pages_list(*, limit: int, timeout_s: float) -> Dict[str, Any]:
    payload = _run_js(_PAGES_LIST_JS, [_bundle_id("Pages"), str(limit)], timeout_s=timeout_s, code="PAGES_LIST_FAILED")
    documents = payload.get("documents") or []
    return {"ok": True, "count": len(documents), "documents": documents,
            "truncated": int(payload.get("total") or 0) > len(documents),
            "note": "Read a document with read_document (item_id = its name; cursor pages through long text)."}


def pages_read(item_id: Optional[str], *, path: Optional[str], cursor: Optional[str], timeout_s: float) -> Dict[str, Any]:
    """A Pages document's body text, MAX_CHARS characters per call (cursor = the first character, 1-based)."""
    first = _cursor(cursor)
    payload = _run_js(_PAGES_READ_JS, [_bundle_id("Pages"), _selector(item_id, path)],
                      timeout_s=max(timeout_s, 15.0), code="PAGES_READ_FAILED")
    text = str(payload.get("text") or "")
    if text and first > len(text):
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"The text has {len(text)} characters.", retryable=False)
    part = text[first - 1:first - 1 + MAX_CHARS]
    end = first - 1 + len(part)
    return {"ok": True, "document": payload.get("document"), "path": payload.get("path"),
            "page_count": payload.get("page_count"), "total_chars": len(text), "text": part,
            "next_cursor": str(end + 1) if end < len(text) else None}
