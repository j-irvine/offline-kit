"""Offline Kit app server.

Serves, on http://127.0.0.1:8765 by default:
  /                         the chat + library web app (app/static)
  /content/<book>/<path>    pages from the offline library (.zim files)
  /api/books                list of collections
  /api/search?q=            full-text search across the library
  /api/suggest?q=           article title suggestions
  /api/chat                 ask the AI (POST; streams Server-Sent Events)
  /api/status               health check

Uses only the Python standard library plus libzim. Talks to Ollama at
127.0.0.1:11435 for the AI model. Nothing here uses the internet.
"""

import argparse
import html
import json
import math
import mimetypes
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.suggestion import SuggestionSearcher

import pdfindex

APP = Path(__file__).resolve().parent
KIT = APP.parents[1]
STATIC = APP / "static"
LIBRARY = KIT / "library"
CONFIG = KIT / "config.json"
OLLAMA = "http://127.0.0.1:11435"


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


# ====================================================================== library


class Book:
    def __init__(self, path):
        self.path = path
        self.archive = Archive(path)
        self.id = re.sub(r"_\d{4}-\d{2}[a-z]?$", "", path.stem)
        self.date = path.stem[len(self.id) + 1:]
        self.title = self.meta("Title") or self.id
        self.description = self.meta("Description")
        self.searchable = self.archive.has_fulltext_index
        self.is_wikipedia = self.id.startswith("wikipedia_")

    def meta(self, key):
        try:
            return bytes(self.archive.get_metadata(key)).decode("utf-8", "replace").strip()
        except (KeyError, RuntimeError):
            return ""

    def info(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "date": self.date,
            "articles": self.archive.article_count,
            "size": self.path.stat().st_size,
            "searchable": self.searchable,
        }


class Library:
    def __init__(self, folder):
        self.books = {}
        for p in sorted(folder.glob("*.zim")):
            try:
                b = Book(p)
                self.books[b.id] = b
            except Exception as e:  # a damaged or unreadable file shouldn't stop the app
                log(f"couldn't open {p.name}: {e}")
        self.by_filename = {str(b.archive.filename): b for b in self.books.values()}
        # Wikipedia first, then the rest alphabetically by title.
        self.ordered = sorted(self.books.values(), key=lambda b: (not b.is_wikipedia, b.title.lower()))
        log(f"library: {len(self.books)} collections")
        self.pdf = pdfindex.PdfSearch(self.books.values(), folder)
        missing = [b for b in self.books.values()
                   if pdfindex.should_index(b.archive) and not pdfindex.index_file(folder, b.path).exists()]
        if missing:
            threading.Thread(target=self._index_pdfs, args=(missing, folder), daemon=True).start()

    def _index_pdfs(self, books, folder):
        for b in books:
            try:
                pdfindex.build(b.archive, b.path, folder, log=log)
            except Exception as e:
                log(f"couldn't index PDFs in {b.id}: {e}")
        self.pdf = pdfindex.PdfSearch(self.books.values(), folder)

    def book_of(self, archive):
        return self.by_filename.get(str(archive.filename))

    def search(self, q, book_ids=None, start=0, count=10):
        """Full-text search; results from several collections are ranked together."""
        books = [self.books[i] for i in (book_ids or []) if i in self.books] or self.ordered
        books = [b for b in books if b.searchable]
        if not books or not q.strip():
            return 0, [], 0
        searcher = Searcher(books[0].archive)
        for b in books[1:]:
            searcher.add_archive(b.archive)
        search = searcher.search(Query().set_query(q))
        total = search.get_estimated_matches()
        out, seen = [], 0
        for r in search.get_results(start, count):
            seen += 1
            b = self.book_of(r.archive)
            if b and not NOISE_PATH.match(r.path):
                out.append({"book": b.id, "bookTitle": b.title, "path": r.path,
                            "title": r.title or r.path, "snippet": clean_snippet(r.snippet)})
        # `seen` is how many results the index returned, including filtered-out ones;
        # the next page starts after them.
        return total, out, start + seen

    def suggest(self, q, count=8):
        out = []
        for b in self.ordered:
            if not b.archive.has_title_index:
                continue
            for path in SuggestionSearcher(b.archive).suggest(q).get_results(0, count):
                try:
                    title = b.archive.get_entry_by_path(path).title
                except KeyError:
                    continue
                out.append({"book": b.id, "bookTitle": b.title, "path": path, "title": title})
            if len(out) >= count:
                break
        return out[:count]

    def resolve(self, book, path):
        """Return (item, redirect_path). Follows ZIM redirects."""
        a = book.archive
        entry = a.main_entry if path == "" else a.get_entry_by_path(path)
        if entry.is_redirect or path == "":
            seen = 0
            while entry.is_redirect and seen < 20:
                entry = entry.get_redirect_entry()
                seen += 1
            if entry.path != path:
                return None, entry.path
        return entry.get_item(), None


# Pages that are lists or profiles rather than content (e.g. Stack Exchange user pages).
NOISE_PATH = re.compile(r"^(users|tags|badges|questions/tagged|questions/?$|about$)(/|$)")


def clean_snippet(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return html.unescape(re.sub(r"\s+", " ", s)).strip()


# ====================================================================== text extraction

BLOCK_TAGS = {"p", "li", "dd", "dt", "tr", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
              "div", "section", "td", "th", "caption", "figcaption", "br"}
SKIP_TAGS = {"head", "title", "script", "style", "sup", "noscript", "button", "nav", "footer", "math", "svg"}
END_SECTIONS = {"references", "external links", "see also", "notes", "further reading",
                "bibliography", "sources", "citations", "footnotes", "notes and references"}


class TextExtractor(HTMLParser):
    """Turns an article's HTML into (section heading, paragraph) pairs."""

    SKIP_CLASSES = ("mw-editsection", "reference", "navbox", "metadata", "mw-ref", "noprint")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.buf = []
        self.section = ""
        self.skip_tag = None  # while set, ignore everything until this element closes
        self.skip_depth = 0
        self.heading = None
        self.done = False

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if not text:
            return
        if self.heading is not None:
            self.section = text
            if text.lower().strip(" .:") in END_SECTIONS:
                self.done = True
        else:
            self.blocks.append((self.section, text))

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        cls = dict(attrs).get("class") or ""
        if tag in SKIP_TAGS or any(c in cls for c in self.SKIP_CLASSES):
            if tag not in ("br", "img", "hr", "input", "meta", "link"):
                self.skip_tag, self.skip_depth = tag, 1
            return
        if tag in BLOCK_TAGS:
            self.flush()
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.heading = tag
        elif tag in ("td", "th"):
            self.buf.append(" | ")

    def handle_endtag(self, tag):
        if self.done:
            return
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in BLOCK_TAGS:
            self.flush()
            if tag == self.heading:
                self.heading = None

    def handle_data(self, data):
        if not self.skip_tag and not self.done:
            self.buf.append(data)


def extract_blocks(raw_html, limit=300_000):
    p = TextExtractor()
    try:
        p.feed(raw_html[:limit])
        p.close()
    except Exception:
        pass
    p.flush()
    return p.blocks


# ====================================================================== retrieval

STOPWORDS = set("""a about above after again against all am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having
he her here hers herself him himself his how i if in into is it its itself just me more most my myself no nor not
now of off on once only or other our ours ourselves out over own same she should so some such than that the their
theirs them themselves then there these they this those through to too under until up very was we were what when
where which while who whom why will with would you your yours yourself yourselves can't don't i'm it's what's
best way ways get got make need tell know want also like use using used much many one thing things""".split())


def stem(w):
    for suf in ("ing", "ies", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def terms(text):
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 1]


def passages_from(blocks, target_words=120, max_passages=120):
    """Merge paragraphs into ~120-word passages within each section."""
    out, cur, cur_sec, n = [], [], None, 0
    for sec, text in blocks:
        if cur and (sec != cur_sec or n >= target_words):
            out.append((cur_sec, " ".join(cur)))
            cur, n = [], 0
        cur_sec = sec
        cur.append(text)
        n += len(text.split())
        if len(out) >= max_passages:
            break
    if cur:
        out.append((cur_sec, " ".join(cur)))
    return out


def retrieve(library, question, queries, max_docs=16, word_budget=1800, per_doc=3):
    """Find the passages in the library most relevant to the question."""
    t0 = time.time()
    candidates = {}

    def run(q, weight):
        try:
            _, results, _ = library.search(q, count=8)
        except Exception as e:
            log("search failed:", q, e)
            return
        for rank, r in enumerate(results):
            key = (r["book"], r["path"])
            prior = weight / (rank + 1)
            if key not in candidates or candidates[key]["prior"] < prior:
                candidates[key] = dict(r, prior=prior, key=key)

    for qi, q in enumerate(queries):
        run(q, 1.0 if qi == 0 else 0.8)
    if len(candidates) < 5:
        # The library's search only returns pages containing every word of a query,
        # so a wordy query can find nothing. Try again with one word left out, then
        # with pairs of words.
        tried, looser = {q.lower() for q in queries}, []
        for q in queries:
            words = [w for w in q.split() if w.lower() not in STOPWORDS] or q.split()
            if len(words) >= 3:
                looser += [" ".join(words[:i] + words[i + 1:]) for i in range(len(words))]
            if len(words) >= 4:
                looser += [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
        for q in looser[:24]:
            if q.lower() not in tried:
                tried.add(q.lower())
                run(q, 0.5)
            if len(candidates) >= 12:
                break
    docs = sorted(candidates.values(), key=lambda d: -d["prior"])[:max_docs]
    t1 = time.time()
    pool = []

    # Pages from PDF guides (their text isn't in Kiwix's search index).
    all_terms = list(dict.fromkeys(re.findall(r"[a-z0-9]+", " ".join([question] + queries).lower())))
    all_terms = [t for t in all_terms if t not in STOPWORDS and len(t) > 2]
    for rank, h in enumerate(library.pdf.search(all_terms, limit=10)):
        book = library.books.get(h["book"])
        if not book:
            continue
        d = {"book": h["book"], "bookTitle": book.title, "path": h["path"], "key": (h["book"], h["path"], h["page"]),
             "group": (h["book"], h["path"]),
             "title": f"{h['title']} (page {h['page']})", "prior": 0.6 / (rank + 1),
             "url": f"/content/{h['book']}/{urllib.parse.quote(h['path'])}#page={h['page']}"}
        blocks = [("", para) for para in re.split(r"\n\s*\n|(?<=[.!?])\n", h["text"]) if para.strip()]
        for sec, text in passages_from(blocks):
            pool.append({"doc": d, "section": sec, "text": re.sub(r"\s+", " ", text), "terms": Counter(terms(text))})

    # Pull the text of each candidate article and split it into passages.
    for d in docs:
        book = library.books[d["book"]]
        try:
            item, redirect = library.resolve(book, d["path"])
            if redirect:
                item, _ = library.resolve(book, redirect)
            if not item or "html" not in item.mimetype:
                continue
            raw = bytes(item.content).decode("utf-8", "replace")
        except Exception:
            continue
        for sec, text in passages_from(extract_blocks(raw)):
            pool.append({"doc": d, "section": sec, "text": text, "terms": Counter(terms(text))})
    t2 = time.time()
    if not pool:
        return []

    # Score passages with BM25 against the question (weighted highest) and the search queries.
    qterms = Counter()
    for t in terms(question):
        qterms[t] += 1.0
    for q in queries:
        for t in terms(q):
            qterms[t] = max(qterms[t], 0.6)
    N = len(pool)
    avg = sum(sum(p["terms"].values()) for p in pool) / N or 1
    df = Counter()
    for p in pool:
        for t in qterms:
            if t in p["terms"]:
                df[t] += 1
    k1, b = 1.2, 0.75
    for p in pool:
        length = sum(p["terms"].values())
        s = 0.0
        for t, w in qterms.items():
            tf = p["terms"].get(t, 0)
            if tf:
                idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                s += w * idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
        title_terms = set(terms(p["doc"]["title"] + " " + (p["section"] or "")))
        s += 0.3 * sum(w for t, w in qterms.items() if t in title_terms)
        s += 1.5 * p["doc"]["prior"]
        p["score"] = s

    # Pick the best passages, a few per article, within the word budget. The same
    # article often appears in several collections (Wikipedia, WikiMed, MDWiki), so
    # articles are grouped by title and repeated text is skipped.
    def norm_title(t):
        return re.sub(r"\W+", " ", re.sub(r"\(page \d+\)$", "", t)).strip().lower()

    chosen, used, per, seen_text = [], 0, defaultdict(int), set()
    for p in sorted(pool, key=lambda p: -p["score"]):
        if p["score"] <= 0.5:
            break
        group = norm_title(p["doc"]["title"])  # all pages of one PDF count as one document
        sig = re.sub(r"\W+", "", p["text"].lower())[:160]
        words = len(p["text"].split())
        if per[group] >= per_doc or used + words > word_budget or sig in seen_text:
            continue
        chosen.append(p)
        seen_text.add(sig)
        per[group] += 1
        used += words
    # Group passages into numbered sources, keeping the order they appear in the article.
    sources, index = [], {}
    for p in chosen:
        key = p["doc"]["title"].strip().lower()
        if key not in index:
            index[key] = len(sources)
            d = p["doc"]
            sources.append({"n": len(sources) + 1, "title": d["title"], "book": d["book"],
                            "bookTitle": d["bookTitle"], "path": d["path"],
                            "url": d.get("url") or f"/content/{d['book']}/{urllib.parse.quote(d['path'])}",
                            "passages": []})
        sources[index[key]]["passages"].append(p)
    for s in sources:
        order = {id(p): i for i, p in enumerate(pool)}
        s["passages"] = [{"section": p["section"], "text": p["text"]}
                         for p in sorted(s["passages"], key=lambda p: order[id(p)])]
    log(f"retrieve: {len(queries)} queries, {len(docs)} docs + PDFs, {N} passages, {len(sources)} sources; "
        f"search {t1 - t0:.2f}s, read {t2 - t1:.2f}s, rank {time.time() - t2:.2f}s")
    return sources


# ====================================================================== AI model

SYSTEM_PROMPT = """You are Offline Kit, a helpful assistant running entirely on this computer, with no internet access. \
You have an offline reference library (Wikipedia, medical references, repair guides, survival guides and more). \
For each question, relevant excerpts from the library are provided as numbered sources.

How to answer:
- Answer the question directly and practically. Lead with the answer, then the important details.
- Base your answer on the sources whenever they are relevant, and cite them inline like [1] or [2][3].
- If the sources don't cover the question, say so briefly, then answer from your own knowledge and make clear it isn't from the library.
- Never invent facts, numbers, doses, or citations. If you're unsure, say so.
- Sources often contain several similar numbers for different purposes (e.g. a strong cleaning solution vs. drinking water, adult vs. child doses). Before giving a quantity, check that it is for exactly the situation asked about. If sources disagree, give the most conservative/official figure and mention the difference.
- For medical, safety, or emergency questions, be careful and specific, mention warning signs that need urgent professional help, and remind the user to verify critical details in the source articles.
- Use short paragraphs and bullet points. Use markdown."""

QUERY_PROMPT = """You turn a user's question into search queries for an offline encyclopedia and reference library.
Write 1 to 3 short keyword queries (2 to 6 words each), like you would type into Wikipedia's search box.
Use the conversation to resolve words like "it" or "that". Prefer standard encyclopedia terms (e.g. "water purification" rather than "make water safe").
Reply with JSON only: {"queries": ["...", "..."]}"""


def keep_awake():
    """Stop the Mac from going to sleep while an answer is being written.

    Held only for the length of one answer (30 minutes at most), so leaving
    Offline Kit open doesn't drain the battery.
    """
    try:
        return subprocess.Popen(["caffeinate", "-i", "-t", "1800"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def ollama_up():
    try:
        urllib.request.urlopen(OLLAMA + "/api/version", timeout=1.5)
        return True
    except OSError:
        return False


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except OSError:
        return []


def model_installed(model, installed):
    full = lambda m: m if ":" in m.rsplit("/", 1)[-1] else m + ":latest"
    return bool(model) and full(model) in {full(m) for m in installed}


def ollama_error(e):
    """A readable message from an Ollama HTTP error."""
    try:
        detail = json.loads(e.read()).get("error", "")
    except Exception:
        detail = ""
    return detail or str(e)


def ollama_chat(model, messages, stream=True, think=False, fmt=None, options=None, timeout=600):
    body = {"model": model, "messages": messages, "stream": stream, "think": think,
            "options": {"num_ctx": 8192, "temperature": 0.3, **(options or {})}}
    if fmt:
        body["format"] = fmt
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def make_queries(model, history, question):
    """Ask the model for search queries; fall back to the question's keywords."""
    fallback = " ".join(w for w in re.findall(r"[A-Za-z0-9'-]+", question)
                        if w.lower() not in STOPWORDS)[:120] or question
    convo = ""
    for m in history[-4:]:
        convo += f"{m['role']}: {m['content'][:500]}\n"
    msgs = [{"role": "system", "content": QUERY_PROMPT},
            {"role": "user", "content": (f"Conversation so far:\n{convo}\n" if convo else "")
             + f"Latest question: {question}"}]
    schema = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
              "required": ["queries"]}
    try:
        with ollama_chat(model, msgs, stream=False, fmt=schema,
                         options={"temperature": 0, "num_predict": 80}, timeout=120) as r:
            content = json.load(r)["message"]["content"]
        qs = [q.strip() for q in json.loads(content).get("queries", []) if isinstance(q, str) and q.strip()]
    except Exception as e:
        log("query generation failed:", e)
        qs = []
    out = []
    for q in qs[:3] + [fallback]:
        if q.lower() not in (x.lower() for x in out):
            out.append(q)
    return out


def format_sources(sources):
    parts = []
    for s in sources:
        body = "\n".join((f"({p['section']}) " if p["section"] else "") + p["text"] for p in s["passages"])
        parts.append(f"[{s['n']}] {s['title']} — from {s['bookTitle']}\n{body}")
    return "\n\n".join(parts)


# ====================================================================== HTTP

TOOLBAR = """<div id="ok-bar" style="position:sticky;top:0;z-index:99999;display:flex;gap:12px;align-items:center;
padding:6px 12px;background:#1f2a24;color:#fff;font:14px -apple-system,system-ui,sans-serif">
<a href="/" style="color:#fff;text-decoration:none;font-weight:600">&#8592; Offline Kit</a>
<span style="opacity:.7">{book}</span>
<form action="/" method="get" style="margin-left:auto;display:flex;gap:6px">
<input type="hidden" name="tab" value="library"><input type="hidden" name="book" value="{book_id}">
<input name="q" placeholder="Search {book}" style="padding:4px 8px;border-radius:6px;border:0;width:220px">
</form></div>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "OfflineKit"
    library = None

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send(code, json.dumps(obj), "application/json")

    def redirect(self, location):
        self.send(302, "", headers={"Location": location})

    # ---- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        arg = lambda k, d="": q.get(k, [d])[0]
        path = urllib.parse.unquote(url.path)
        try:
            if path in ("/", "/index.html"):
                return self.static("index.html")
            if path.startswith("/static/"):
                return self.static(path[len("/static/"):])
            if path.startswith("/content/"):
                return self.content(path[len("/content/"):], url.query)
            if path == "/api/status":
                cfg = load_config()
                up = ollama_up()
                model = cfg.get("model", "")
                return self.send_json({"books": len(self.library.books), "model": model, "ollama": up,
                                       "modelReady": up and model_installed(model, ollama_models())})
            if path == "/api/books":
                return self.send_json([b.info() for b in self.library.ordered])
            if path == "/api/illustration":
                b = self.library.books.get(arg("book"))
                try:
                    item = b.archive.get_illustration_item()
                    return self.send(200, bytes(item.content), item.mimetype or "image/png",
                                     {"Cache-Control": "max-age=86400"})
                except Exception:
                    return self.send(404)
            if path == "/api/search":
                books = [x for x in arg("book").split(",") if x]
                try:
                    start = max(0, int(arg("start", "0") or 0))
                except ValueError:
                    start = 0
                total, results, nxt = self.library.search(arg("q"), books, start, 20)
                pdfs = []
                if start == 0:
                    words = [t for t in re.findall(r"[a-z0-9]+", arg("q").lower()) if t not in STOPWORDS]
                    for h in self.library.pdf.search(words, limit=30):
                        if (books and h["book"] not in books) or any(p["path"] == h["path"] for p in pdfs):
                            continue  # one result per PDF (its best page)
                        b = self.library.books[h["book"]]
                        snippet = re.sub(r"\s+", " ", h["text"])[:260]
                        pdfs.append({"title": h["title"], "bookTitle": b.title, "page": h["page"], "path": h["path"],
                                     "snippet": snippet + "…",
                                     "url": f"/content/{b.id}/{urllib.parse.quote(h['path'])}#page={h['page']}"})
                        if len(pdfs) == 5:
                            break
                return self.send_json({"total": total, "results": results, "pdfs": pdfs,
                                       "next": nxt if nxt < total and nxt > start else None})
            if path == "/api/suggest":
                return self.send_json(self.library.suggest(arg("q")))
            if path == "/api/random":
                b = self.library.books.get(arg("book")) or self.library.ordered[0]
                e = b.archive.get_random_entry()
                return self.redirect(f"/content/{b.id}/{urllib.parse.quote(e.path)}")
            self.send(404, "Not found")
        except BrokenPipeError:
            pass
        except Exception:
            log(traceback.format_exc())
            try:
                self.send(500, "Something went wrong")
            except Exception:
                pass

    def do_POST(self):
        if self.path == "/api/chat":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                messages = [{"role": m["role"], "content": str(m["content"])} for m in req["messages"]
                            if m.get("role") in ("user", "assistant") and m.get("content")]
            except (ValueError, KeyError, TypeError, AttributeError):
                return self.send(400, "Bad request")
            return self.chat(req, messages)
        self.send(404, "Not found")

    # ---- static files
    def static(self, name):
        p = (STATIC / name).resolve()
        if STATIC.resolve() not in p.parents or not p.is_file():
            return self.send(404, "Not found")
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send(200, p.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    # ---- library pages
    def content(self, rest, query):
        book_id, _, zpath = rest.partition("/")
        book = self.library.books.get(book_id)
        if not book:
            return self.send(404, "Collection not found")
        try:
            item, redirect = self.library.resolve(book, zpath)
        except KeyError:
            return self.send(404, "Page not found in this collection")
        if redirect is not None:
            return self.redirect(f"/content/{book_id}/{urllib.parse.quote(redirect)}")
        body = bytes(item.content)
        ctype = item.mimetype or "application/octet-stream"
        if ctype.startswith("text/html"):
            bar = TOOLBAR.format(book=html.escape(book.title), book_id=html.escape(book.id)).replace("\n", " ")
            text = body.decode("utf-8", "replace")
            m = re.search(r"<body[^>]*>", text, re.I)
            text = text[: m.end()] + bar + text[m.end():] if m else bar + text
            body = text.encode()
            ctype = "text/html; charset=utf-8"
        self.send(200, body, ctype, {"Cache-Control": "max-age=3600"})

    # ---- chat (Server-Sent Events)
    def event(self, kind, **data):
        self.wfile.write(f"data: {json.dumps(dict(type=kind, **data))}\n\n".encode())
        self.wfile.flush()

    def chat(self, req, messages):
        if not messages or messages[-1]["role"] != "user":
            return self.send(400, "No question")
        question = messages[-1]["content"].strip()
        history = messages[:-1]
        use_library = req.get("useLibrary", True)
        think = bool(req.get("think", False))
        model = load_config().get("model", "")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        awake = keep_awake()
        try:
            if not ollama_up():
                return self.event("error", text="The AI engine isn't running. Close Offline Kit and start it again.")
            sources = []
            if use_library and self.library.books:
                self.event("status", text="Searching the library…")
                queries = make_queries(model, history, question)
                self.event("status", text="Reading articles: " + "; ".join(queries[:3]))
                sources = retrieve(self.library, question, queries)
                self.event("sources", queries=queries,
                           sources=[{k: s[k] for k in ("n", "title", "bookTitle", "url")} for s in sources])
            self.event("status", text="Thinking…" if think else "Writing…")

            msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
            for m in history[-6:]:
                msgs.append({"role": m["role"], "content": m["content"]})
            if sources:
                content = (f"Library sources:\n\n{format_sources(sources)}\n\n---\n\n"
                           f"Question: {question}\n\nAnswer using the sources above, citing them like [1].")
            elif use_library:
                content = (f"(The library search found nothing relevant for this question.)\n\nQuestion: {question}")
            else:
                content = question
            msgs.append({"role": "user", "content": content})

            if not model:
                return self.event("error", text="No AI model is set up yet. Connect to the internet and run the "
                                  "installer again. The Library tab works without it.")
            try:
                r = ollama_chat(model, msgs, think=think)
            except urllib.error.HTTPError as e:
                detail = ollama_error(e)
                log("chat: ollama refused:", e.code, detail)
                if think and "think" in detail.lower():
                    # This model can't show its reasoning; answer normally instead.
                    r = ollama_chat(model, msgs, think=False)
                elif e.code == 404:
                    return self.event("error", text=f"The AI model ({model}) isn't installed. Connect to the "
                                      "internet and run the installer again. The Library tab works without it.")
                else:
                    return self.event("error", text=f"The AI engine reported a problem: {detail}")
            with r:
                for line in r:
                    msg = json.loads(line)
                    if "error" in msg:
                        return self.event("error", text=msg["error"])
                    m = msg.get("message", {})
                    if m.get("thinking"):
                        self.event("thinking", text=m["thinking"])
                    if m.get("content"):
                        self.event("token", text=m["content"])
                    if msg.get("done"):
                        stats = {"tokens": msg.get("eval_count"),
                                 "seconds": round((msg.get("eval_duration") or 0) / 1e9, 1)}
                        self.event("done", stats=stats)
        except (BrokenPipeError, ConnectionResetError):
            log("chat: browser disconnected (stopped)")
        except Exception as e:
            log(traceback.format_exc())
            try:
                self.event("error", text=f"Something went wrong: {e}")
            except Exception:
                pass
        finally:
            if awake:
                awake.terminate()
                awake.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        log(f"couldn't start on port {args.port}: {e}")
        sys.exit(2)
    Handler.library = Library(LIBRARY)
    server.daemon_threads = True
    log(f"serving on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
