"""Search index for PDFs stored inside library collections.

Some collections (e.g. the "zimgit" survival guides) are folders of PDFs, and
Kiwix's built-in search doesn't look inside PDFs. This module extracts their
text once, page by page, into a small SQLite full-text index next to the
library, so the AI and the search page can use them.

Indexes live in library/.pdfindex/<zim file name>.sqlite and are rebuilt
automatically when a collection is updated (the file name changes).
"""

import io
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

logging.getLogger("pypdf").setLevel(logging.ERROR)

INDEX_VERSION = 2
_build_lock = threading.Lock()


def pdf_count(archive):
    try:
        counter = bytes(archive.get_metadata("Counter")).decode()
    except (KeyError, RuntimeError):
        return 0
    m = re.search(r"application/pdf=(\d+)", counter)
    return int(m.group(1)) if m else 0


def should_index(archive):
    """Index PDFs only where they're a real part of the collection.

    Skips huge collections (scanning millions of entries is slow) and ones where
    PDFs are a rare extra, like the few dozen PDFs inside Wikipedia.
    """
    pdfs = pdf_count(archive)
    if not pdfs or archive.all_entry_count > 1_000_000:
        return False
    try:
        counter = bytes(archive.get_metadata("Counter")).decode()
    except (KeyError, RuntimeError):
        return False
    html = sum(int(n) for n in re.findall(r"text/html[^=;]*=(\d+)", counter))
    return pdfs * 200 >= html


def index_file(library_dir, zim_path):
    return Path(library_dir) / ".pdfindex" / f"{Path(zim_path).stem}.v{INDEX_VERSION}.sqlite"


def _title_for(reader, path):
    title = ""
    try:
        title = (reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:
        pass
    junk = re.compile(r"(microsoft (word|powerpoint|excel)|untitled|\.docx?$|\.pdf$|^\W*$|^[\w.-]*\d[\w.-]*$)", re.I)
    if not title or junk.search(title) or len(title) < 4 or " " not in title:
        title = ""
        for page in reader.pages[:3]:  # the first page is sometimes a blank cover
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
            lines = [l for l in lines if len(l) > 3 and re.search(r"[A-Za-z]{3}", l)]
            if lines:
                title = " ".join(lines[:2])
                title = title[:80].rsplit(" ", 1)[0] + "…" if len(title) > 80 else title
                break
    return title or Path(path).stem


def build(archive, zim_path, library_dir, log=print):
    """Extract every PDF's text into the index for this collection. Safe to call repeatedly."""
    from pypdf import PdfReader

    dest = index_file(library_dir, zim_path)
    if dest.exists():
        return dest
    with _build_lock:
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Remove indexes for older versions of this collection.
        prefix = re.sub(r"_\d{4}-\d{2}[a-z]?$", "", Path(zim_path).stem)
        for old in dest.parent.glob(prefix + "_*.sqlite"):
            old.unlink()
        tmp = dest.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        db = sqlite3.connect(tmp)
        db.execute("CREATE VIRTUAL TABLE pages USING fts5(title, text, path UNINDEXED, page UNINDEXED, "
                   "tokenize='porter unicode61')")
        t0, docs, pages = time.time(), 0, 0
        for i in range(archive.all_entry_count):
            try:
                entry = archive.get_entry_by_id(i)
                if entry.is_redirect:
                    continue
                item = entry.get_item()
                if item.mimetype != "application/pdf":
                    continue
                reader = PdfReader(io.BytesIO(bytes(item.content)))
                title = _title_for(reader, entry.path)
                rows = []
                for n, page in enumerate(reader.pages, 1):
                    try:
                        text = re.sub(r"[ \t]+", " ", page.extract_text() or "").strip()
                    except Exception:
                        continue
                    if len(text) > 40:
                        rows.append((title, text, entry.path, n))
                db.executemany("INSERT INTO pages VALUES (?, ?, ?, ?)", rows)
                docs += 1
                pages += len(rows)
            except Exception as e:  # one unreadable PDF shouldn't stop the rest
                log(f"  skipped a PDF ({e.__class__.__name__})")
        db.execute("PRAGMA user_version = %d" % INDEX_VERSION)
        db.commit()
        db.execute("INSERT INTO pages(pages) VALUES('optimize')")
        db.commit()
        db.close()
        tmp.rename(dest)
        log(f"  indexed {docs} PDFs ({pages} pages) from {Path(zim_path).name} in {time.time() - t0:.0f}s")
        return dest


class PdfSearch:
    """Searches all built PDF indexes."""

    def __init__(self, books, library_dir):
        self.dbs = {}  # book id -> sqlite path
        for b in books:
            p = index_file(library_dir, b.path)
            if p.exists():
                self.dbs[b.id] = p

    def search(self, terms, limit=10):
        """terms: list of words. Returns pages ranked by BM25 (best first)."""
        words = [re.sub(r'["*^:()]', "", t) for t in terms]
        words = [w for w in words if w]
        if not words or not self.dbs:
            return []
        match = " OR ".join(f'"{w}"' for w in words)
        hits = []
        for book_id, path in self.dbs.items():
            try:
                db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                rows = db.execute(
                    "SELECT title, text, path, page, bm25(pages, 4.0, 1.0) AS score FROM pages "
                    "WHERE pages MATCH ? ORDER BY score LIMIT ?", (match, limit)).fetchall()
                db.close()
            except sqlite3.Error:
                continue
            for title, text, zpath, page, score in rows:
                hits.append({"book": book_id, "title": title, "text": text, "path": zpath,
                             "page": page, "score": -score})
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]
