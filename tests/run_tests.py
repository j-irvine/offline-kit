"""Offline Kit self-test.

Checks the installer logic, the library server, search, and the chat pipeline
against a small made-up library, a stand-in AI engine and a stand-in download
server. It needs no internet, downloads nothing, and works in a temporary
folder, so it never touches a real installation.

Run it with the kit's own Python:

    ~/OfflineKit/venv/bin/python ~/OfflineKit/kit/tests/run_tests.py
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="offline-kit-test-"))
KIT = TMP / "OfflineKit"
os.environ["HOME"] = str(TMP / "home")
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
(TMP / "home" / "Desktop").mkdir(parents=True)
(KIT / "library").mkdir(parents=True)
shutil.copytree(REPO, KIT / "kit", ignore=shutil.ignore_patterns(".git", "__pycache__"))
sys.path[:0] = [str(KIT / "kit" / "app"), str(REPO / "tests")]

import fakes  # noqa: E402
import fixtures  # noqa: E402
import pdfindex  # noqa: E402
import server  # noqa: E402
import setup  # noqa: E402

assert server.KIT == KIT and setup.KIT == KIT, "the tests must run against the temporary copy"


@contextlib.contextmanager
def quiet_fd():
    """Hide output written straight to the terminal by libzim and curl."""
    sys.stdout.flush()
    saved = [os.dup(1), os.dup(2)]
    with open(os.devnull, "w") as null:
        os.dup2(null.fileno(), 1)
        os.dup2(null.fileno(), 2)
    try:
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])


def get(base, path, raw=False):
    """GET without following redirects. Returns (status, headers, body)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + path, timeout=30) as r:
            body = r.read()
            return r.status, r.headers, body if raw else body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, e.headers, body if raw else body.decode("utf-8", "replace")


def chat(base, payload, timeout=60):
    """POST /api/chat. Returns (status, list of events)."""
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(base + "/api/chat", data=data, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            text = r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, []
    return 200, [json.loads(c[6:]) for c in text.split("\n\n") if c.startswith("data: ")]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ====================================================================== server


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with quiet_fd():
            fixtures.build_all(KIT / "library")
        (KIT / "config.json").write_text(json.dumps({"tier": "essentials", "model": "bonsai-2:4b"}))
        cls.ollama, host = fakes.serve(fakes.FakeOllama)
        server.OLLAMA = "http://" + host
        server.log = lambda *a: None
        server.keep_awake = lambda: None
        server.Handler.library = cls.lib = server.Library(KIT / "library")
        for _ in range(100):  # the PDF index is built in the background on first start
            if cls.lib.pdf.dbs:
                break
            time.sleep(0.1)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.http.daemon_threads = True
        import threading
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"

    def setUp(self):
        fakes.FakeOllama.requests.clear()
        fakes.FakeOllama.no_thinking = False
        fakes.FakeOllama.models[:] = ["bonsai-2:4b"]

    def json(self, path):
        code, _, body = get(self.base, path)
        self.assertEqual(code, 200, path)
        return json.loads(body)

    # ---- library
    def test_damaged_collection_is_skipped(self):
        self.assertEqual(sorted(self.lib.books), ["wikipedia_en_test", "zimgit-test_en"])

    def test_books_wikipedia_first(self):
        books = self.json("/api/books")
        self.assertEqual([b["id"] for b in books], ["wikipedia_en_test", "zimgit-test_en"])
        self.assertEqual(books[0]["title"], "Test Wikipedia")
        self.assertEqual(books[0]["date"], "2026-01")

    def test_status(self):
        s = self.json("/api/status")
        self.assertEqual(s, {"books": 2, "model": "bonsai-2:4b", "ollama": True, "modelReady": True})

    def test_model_names_with_and_without_tag(self):
        self.assertTrue(server.model_installed("llama", ["llama:latest"]))
        self.assertTrue(server.model_installed("llama:latest", ["llama"]))
        self.assertFalse(server.model_installed("llama:7b", ["llama:latest"]))
        self.assertFalse(server.model_installed("", ["llama:latest"]))

    def test_app_pages(self):
        for path, needle in [("/", "<title>Offline Kit</title>"), ("/static/app.js", "use strict"),
                             ("/static/style.css", "{")]:
            code, _, body = get(self.base, path)
            self.assertEqual(code, 200)
            self.assertIn(needle, body)

    def test_app_uses_nothing_from_the_internet(self):
        for f in (KIT / "kit" / "app" / "static").iterdir():
            text = f.read_text()
            for bad in ("https://", "http://", "//cdn", "@import"):
                self.assertNotIn(bad, text.replace("http://www.w3.org/2000/svg", ""), f"{f.name} refers to {bad}")

    def test_static_cannot_escape_its_folder(self):
        for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2f..%2fcatalog.tsv",
                     "/static/", "/static/nope.js"):
            self.assertEqual(get(self.base, path)[0], 404, path)

    def test_main_page_redirect(self):
        for path in ("/content/wikipedia_en_test", "/content/wikipedia_en_test/"):
            code, headers, _ = get(self.base, path)
            self.assertEqual((code, headers["Location"]), (302, "/content/wikipedia_en_test/Main_Page"))

    def test_redirect_entry(self):
        code, headers, _ = get(self.base, "/content/wikipedia_en_test/Purifying_water")
        self.assertEqual((code, headers["Location"]), (302, "/content/wikipedia_en_test/Water_purification"))

    def test_article_gets_toolbar(self):
        code, headers, body = get(self.base, "/content/wikipedia_en_test/Water_purification")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn('id="ok-bar"', body)
        self.assertLess(body.index("<body"), body.index('id="ok-bar"'))
        self.assertIn("rolling boil", body)

    def test_page_without_body_tag(self):
        code, _, body = get(self.base, "/content/wikipedia_en_test/NoBody")
        self.assertEqual(code, 200)
        self.assertIn('id="ok-bar"', body)
        self.assertIn("zebra", body)

    def test_unusual_titles(self):
        for path, needle in [("Café_(drink)", "espresso"), ("AC/DC", "rock band"),
                             ("What_is_100%_juice?", "oranges")]:
            code, _, body = get(self.base, "/content/wikipedia_en_test/" + urllib.parse.quote(path))
            self.assertEqual(code, 200, path)
            self.assertIn(needle, body)

    def test_images_and_pdfs_are_served_unchanged(self):
        code, headers, body = get(self.base, "/content/wikipedia_en_test/img/dot.png", raw=True)
        self.assertEqual((code, headers["Content-Type"], body), (200, "image/png", fixtures.PNG))
        code, headers, body = get(self.base, "/content/zimgit-test_en/files/knots%20guide.pdf", raw=True)
        self.assertEqual((code, headers["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(body.startswith(b"%PDF"))

    def test_missing_things_are_404(self):
        for path in ("/content/wikipedia_en_test/Nope", "/content/nobook/x", "/nothing", "/api/nothing",
                     "/api/illustration", "/api/illustration?book=zimgit-test_en"):
            self.assertEqual(get(self.base, path)[0], 404, path)

    def test_illustration(self):
        code, _, body = get(self.base, "/api/illustration?book=wikipedia_en_test", raw=True)
        self.assertEqual((code, body), (200, fixtures.PNG))

    def test_random(self):
        code, headers, _ = get(self.base, "/api/random?book=wikipedia_en_test")
        self.assertEqual(code, 302)
        code, headers, _ = get(self.base, headers["Location"])
        if code == 302:  # the random page can itself be a redirect to another article
            code = get(self.base, headers["Location"])[0]
        self.assertEqual(code, 200)

    # ---- search
    def test_search(self):
        r = self.json("/api/search?q=water+purification")
        self.assertEqual([x["path"] for x in r["results"]], ["Water_purification"])  # the "users/123" page is hidden
        self.assertNotIn("<", r["results"][0]["snippet"])
        self.assertIsNone(r["next"])

    def test_search_one_collection(self):
        self.assertEqual(self.json("/api/search?q=water+purification&book=zimgit-test_en")["results"], [])
        self.assertEqual(len(self.json("/api/search?q=water+purification&book=wikipedia_en_test")["results"]), 1)

    def test_search_finds_text_inside_pdfs(self):
        r = self.json("/api/search?q=bowline+loop")
        self.assertEqual(len(r["pdfs"]), 1)
        self.assertEqual(r["pdfs"][0]["url"], "/content/zimgit-test_en/files/knots%20guide.pdf#page=2")
        self.assertEqual(get(self.base, r["pdfs"][0]["url"].split("#")[0])[0], 200)
        self.assertIn("fixed loop", r["pdfs"][0]["snippet"])

    def test_search_odd_input(self):
        for q in ("", "%20", "%22unbalanced+AND+(", "a*b:c^", "water&start=abc", "water&start=-5",
                  "water&start=999999", "%F0%9F%92%A7", "x" * 3000):
            code, _, body = get(self.base, "/api/search?q=" + q)
            self.assertEqual(code, 200, q[:40])
            self.assertIn("results", json.loads(body))

    def test_search_paging_offsets(self):
        total, results, nxt = self.lib.search("water", count=1)
        self.assertEqual(nxt, 1)
        seen = [r["path"] for r in results]
        while nxt < total:
            before = nxt
            _, results, nxt = self.lib.search("water", start=nxt, count=1)
            seen += [r["path"] for r in results]
            if nxt == before:
                break
        self.assertEqual(len(seen), len(set(seen)), "a result was shown twice")
        self.assertIn("Water_purification", seen)

    def test_suggest(self):
        titles = [s["title"] for s in self.json("/api/suggest?q=hypo")]
        self.assertEqual(titles, ["Hypothermia"])
        self.assertEqual(self.json("/api/suggest?q=zzzzqq"), [])

    # ---- reading articles for the AI
    def test_text_extraction_skips_page_furniture(self):
        blocks = server.extract_blocks(fixtures.WATER)
        text = " ".join(t for _, t in blocks)
        for junk in ("NAVBOXJUNK", "SCRIPTJUNK", "REFERENCEJUNK", "[1]"):
            self.assertNotIn(junk, text)
        self.assertNotIn(("", "Water purification"), blocks, "the page <title> was read as a paragraph")
        self.assertIn("Boiling", [s for s, _ in blocks])
        self.assertIn("2 drops", text)

    def test_text_extraction_survives_bad_html(self):
        for bad in ("", "<p>unclosed <b>tags", "<<<>>>", "<p class='noprint'>never closed", "\x00\x01",
                    "<table><tr><td>a<td>b</table>", "plain text only"):
            server.extract_blocks(bad)

    def test_retrieve_finds_the_article(self):
        sources = server.retrieve(self.lib, "How do I purify water from a stream?", ["water purification"])
        self.assertEqual(sources[0]["title"], "Water purification")
        self.assertEqual(sources[0]["url"], "/content/wikipedia_en_test/Water_purification")
        text = server.format_sources(sources)
        self.assertIn("rolling boil", text)
        self.assertNotIn("JUNK", text)
        self.assertEqual([s["n"] for s in sources], list(range(1, len(sources) + 1)))

    def test_retrieve_when_queries_are_too_wordy(self):
        # No page contains all of these words; the search should loosen up rather than find nothing.
        sources = server.retrieve(self.lib, "How do I purify water from a stream?",
                                  ["stream water purification filtration techniques"])
        self.assertIn("Water purification", [s["title"] for s in sources])

    def test_retrieve_uses_pdf_pages(self):
        sources = server.retrieve(self.lib, "how do I tie a bowline", ["bowline knot"])
        self.assertTrue(sources)
        self.assertIn("(page 2)", sources[0]["title"])
        self.assertTrue(sources[0]["url"].endswith("knots%20guide.pdf#page=2"))

    def test_retrieve_nothing_relevant(self):
        for q in ("quantum chromodynamics lagrangian", "???", ""):
            self.assertEqual(server.retrieve(self.lib, q, [q]), [])

    def test_pdf_search_prefers_pages_with_all_the_words(self):
        hits = self.lib.pdf.search(["loop", "post"])  # "loop" is on pages 2, "post" on page 3; none has both
        self.assertEqual({h["page"] for h in hits}, {2, 3})
        hits = self.lib.pdf.search(["fixed", "loop", "stream"])
        self.assertEqual((hits[0]["path"], hits[0]["page"]), ("files/knots guide.pdf", 2))
        hits = self.lib.pdf.search(["clove", "hitch", "latrine"])
        self.assertEqual(len(hits), 2)

    def test_pdf_titles(self):
        hits = self.lib.pdf.search(["bowline"])
        self.assertTrue(hits[0]["title"].startswith("Essential Knots Field Guide"))  # not "Microsoft Word - ..."
        self.assertEqual(self.lib.pdf.search(["latrine"])[0]["title"], "Emergency Sanitation Guide")
        self.assertEqual(self.lib.pdf.search(['"', "*", "(", ""]), [])

    # ---- chat
    def test_chat_with_library(self):
        code, events = chat(self.base, {"messages": [{"role": "user", "content": "How do I purify water?"}],
                                        "useLibrary": True, "think": False})
        self.assertEqual(code, 200)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "status")
        self.assertLess(kinds.index("sources"), kinds.index("token"))
        self.assertEqual(kinds[-1], "done")
        sources = next(e for e in events if e["type"] == "sources")["sources"]
        self.assertEqual(sources[0]["title"], "Water purification")
        for s in sources:
            self.assertEqual(get(self.base, s["url"].split("#")[0])[0], 200, "a source link is broken")
        answer = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertEqual(answer, "".join(fakes.FakeOllama.answer))
        self.assertEqual(events[-1]["stats"], {"tokens": 12, "seconds": 2.0})
        ask, final = fakes.FakeOllama.requests
        self.assertIn("format", ask)                     # first the search queries...
        self.assertIs(ask["think"], False)
        self.assertEqual(final["messages"][0]["role"], "system")
        self.assertIn("[1] Water purification", final["messages"][-1]["content"])
        self.assertIn("rolling boil", final["messages"][-1]["content"])
        self.assertIn("Question: How do I purify water?", final["messages"][-1]["content"])

    def test_chat_without_library(self):
        _, events = chat(self.base, {"messages": [{"role": "user", "content": "hello"}], "useLibrary": False})
        self.assertNotIn("sources", [e["type"] for e in events])
        self.assertEqual(len(fakes.FakeOllama.requests), 1)
        self.assertEqual(fakes.FakeOllama.requests[0]["messages"][-1]["content"], "hello")

    def test_chat_passes_earlier_messages(self):
        history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply"},
                   {"role": "assistant", "content": ""},  # an answer that was stopped before it began
                   {"role": "user", "content": "second"}]
        chat(self.base, {"messages": history, "useLibrary": False})
        sent = fakes.FakeOllama.requests[0]["messages"]
        self.assertEqual([(m["role"], m["content"]) for m in sent[1:]],
                         [("user", "first"), ("assistant", "reply"), ("user", "second")])

    def test_chat_think_longer(self):
        _, events = chat(self.base, {"messages": [{"role": "user", "content": "hi"}], "useLibrary": False,
                                     "think": True})
        self.assertIn("thinking", [e["type"] for e in events])
        self.assertIs(fakes.FakeOllama.requests[-1]["think"], True)

    def test_chat_think_longer_with_a_model_that_cannot(self):
        fakes.FakeOllama.no_thinking = True
        _, events = chat(self.base, {"messages": [{"role": "user", "content": "hi"}], "useLibrary": False,
                                     "think": True})
        self.assertEqual([e["type"] for e in events if e["type"] in ("error", "done")], ["done"])
        self.assertTrue("".join(e["text"] for e in events if e["type"] == "token"))

    def test_chat_model_not_installed(self):
        fakes.FakeOllama.models[:] = []
        _, events = chat(self.base, {"messages": [{"role": "user", "content": "How do I purify water?"}]})
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("isn't installed", events[-1]["text"])
        self.assertFalse(self.json("/api/status")["modelReady"])

    def test_chat_engine_not_running(self):
        saved, server.OLLAMA = server.OLLAMA, f"http://127.0.0.1:{free_port()}"
        try:
            _, events = chat(self.base, {"messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual([e["type"] for e in events], ["error"])
            self.assertIn("isn't running", events[0]["text"])
            self.assertFalse(self.json("/api/status")["ollama"])
            # The library still works without the AI engine.
            self.assertEqual(len(self.json("/api/search?q=hypothermia")["results"]), 1)
        finally:
            server.OLLAMA = saved

    def test_chat_bad_requests(self):
        for payload in (b"not json", b"[1]", b"{}", b'{"messages": "x"}', b'{"messages": [{"role": "user"}]}',
                        b'{"messages": [{"role": "assistant", "content": "x"}]}', b'{"messages": [3]}'):
            self.assertEqual(chat(self.base, payload)[0], 400, payload)

    def test_query_fallback_when_model_fails(self):
        saved, server.OLLAMA = server.OLLAMA, f"http://127.0.0.1:{free_port()}"
        try:
            self.assertEqual(server.make_queries("m", [], "How do I purify water from a stream?"),
                             ["purify water stream"])
        finally:
            server.OLLAMA = saved


# ====================================================================== installer


CATALOG = """# test catalog
tiers\tdir\tprefix\ttitle\tabout
E\twikipedia\twikipedia_en_all_mini\tWikipedia (summaries)\tintro only
S\twikipedia\twikipedia_en_all_nopic\tWikipedia (full text)\tfull
ESC\tother\tzimgit-test_en\tGuides\tPDF guides
SC\tother\tgone_en_all\tGone\tno longer on the server
"""


def blob(seed, size):
    out = hashlib.sha256(seed.encode()).digest()
    return (out * (size // len(out) + 1))[:size]


class SetupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kiwix, khost = fakes.serve(fakes.FakeKiwix)
        cls.ollama, ohost = fakes.serve(fakes.FakeOllama)
        setup.MIRROR = f"http://{khost}/zim"
        setup.MIRRORS = [setup.MIRROR, f"http://127.0.0.1:{free_port()}/zim"]  # the second one is down
        setup.OLLAMA_HOST = ohost
        setup.ram_bytes = lambda: 16 * 1024**3
        setup.set_icon = lambda path: None
        setup.time.sleep = lambda s: None
        cls.real_catalog = (REPO / "catalog.tsv").read_text()

    def setUp(self):
        self.lib = setup.LIBRARY = KIT / "library-setup"
        shutil.rmtree(self.lib, ignore_errors=True)
        self.lib.mkdir()
        (KIT / "kit" / "catalog.tsv").write_text(CATALOG)
        setup.CONFIG = KIT / "config-setup.json"
        setup.CONFIG.unlink(missing_ok=True)
        with quiet_fd():
            guides = self.lib / "g.zim"
            fixtures.build_guides(guides)
        K = fakes.FakeKiwix
        K.files = {
            "wikipedia/wikipedia_en_all_mini_2025-11.zim": blob("old mini", 5000),
            "wikipedia/wikipedia_en_all_mini_2026-02.zim": blob("mini", 300_000),
            "wikipedia/wikipedia_en_all_nopic_2026-01.zim": blob("nopic", 900_000),
            "wikipedia/wikipedia_en_all_maxi_2026-01.zim": blob("maxi", 2000),
            "other/zimgit-test_en_2026-02.zim": guides.read_bytes(),
        }
        guides.unlink()
        K.corrupt_once, K.fail_meta, K.hits = set(), set(), []
        fakes.FakeOllama.models[:] = []
        fakes.FakeOllama.pulled.clear()
        self.out = io.StringIO()
        self._say, setup.say = setup.say, lambda msg="": self.out.write(str(msg) + "\n")
        self._usage = shutil.disk_usage

    def tearDown(self):
        setup.say = self._say
        shutil.disk_usage = self._usage

    def rows(self):
        return setup.resolve_catalog(setup.read_catalog())

    def row(self, prefix):
        return next(r for r in self.rows()[0] if r["prefix"] == prefix)

    def run_main(self, *args):
        argv, sys.argv = sys.argv, ["setup.py", *args]
        try:
            with quiet_fd():
                return setup.main()
        finally:
            sys.argv = argv

    def names(self):
        return sorted(p.name for p in self.lib.iterdir() if not p.name.startswith("."))

    # ---- the real catalog
    def test_real_catalog_is_well_formed(self):
        (KIT / "kit" / "catalog.tsv").write_text(self.real_catalog)
        rows = setup.read_catalog()
        self.assertGreater(len(rows), 30)
        self.assertEqual(self.out.getvalue(), "", "a catalog line was ignored")
        self.assertEqual(len({r["prefix"] for r in rows}), len(rows), "a collection is listed twice")
        for r in rows:
            self.assertTrue(r["tiers"] and set(r["tiers"]) <= set("ESC"), r)
            self.assertRegex(r["dir"], r"^[a-z_]+$")
            self.assertRegex(r["prefix"], r"^[A-Za-z0-9._-]+$")
            self.assertTrue(r["title"] and r["about"])
        for tier in setup.TIERS:
            in_tier = [r["prefix"] for r in setup.tier_rows(rows, tier)]
            wikis = [p for p in in_tier if p.startswith("wikipedia_en_all_")]
            self.assertEqual(len(wikis), 1, f"{tier} should have exactly one full Wikipedia")
            self.assertIn("wikipedia_en_medicine_maxi", in_tier)

    # ---- reading the server's file list
    def test_listing_formats(self):
        nginx = ('<a href="../">../</a>\n'
                 '<a href="wikipedia_en_all_maxi_2026-01.zim">wikipedia_en_all_maxi_2026-01.zim</a>'
                 '                  2026-01-20 08:11  115G\n'
                 '<a href="zimgit-knots_en_2024-08.zim">zimgit-knots_en_2024-08.zim</a>     2024-08-06 02:19   27M\n'
                 '<a href="tiny_en_2024-08.zim">tiny_en_2024-08.zim</a>     2024-08-06 02:19   512\n'
                 '<a href="readme.txt">readme.txt</a>     2024-08-06 02:19   1K\n')
        self.assertEqual(setup.parse_listing(nginx), {
            "wikipedia_en_all_maxi_2026-01.zim": 115 * 1000**3, "zimgit-knots_en_2024-08.zim": 27 * 1000**2,
            "tiny_en_2024-08.zim": 512})
        nginx_default = ('<a href="zimgit-knots_en_2024-08.zim">zimgit-knots_en_2024-08.zim</a>'
                         '                        06-Aug-2024 02:19            27540123\r\n'
                         '<a href="b_en_2024-08.zim">b_en_2024-08.zim</a>   06-Aug-2024 02:19     1.2G\r\n')
        self.assertEqual(setup.parse_listing(nginx_default),
                         {"zimgit-knots_en_2024-08.zim": 27540123, "b_en_2024-08.zim": int(1.2 * 1000**3)})
        apache = ('<tr><td><a href="a_en_2026-01.zim">a_en_2026-01.zim</a></td><td align="right">2026-01-20 08:11  </td>'
                  '<td align="right">1.5G</td></tr>')
        self.assertEqual(setup.parse_listing(apache), {"a_en_2026-01.zim": int(1.5 * 1000**3)})
        # A layout we've never seen: the files are still found, with the size unknown.
        other = '<li><a class="f" href="a_en_2026-01.zim">a</a> <span>big</span></li><li><a href="b%20c_2026-01.zim">b</a>'
        self.assertEqual(setup.parse_listing(other), {"a_en_2026-01.zim": 0, "b c_2026-01.zim": 0})

    def test_newest_version_is_chosen(self):
        rows, missing = self.rows()
        self.assertEqual(self.row("wikipedia_en_all_mini")["file"], "wikipedia_en_all_mini_2026-02.zim")
        self.assertEqual(self.row("wikipedia_en_all_mini")["approx_size"], 300_000)
        self.assertEqual([r["prefix"] for r in missing], ["gone_en_all"])
        self.assertEqual(len(rows), 3)

    def test_server_unreachable_is_a_plain_message(self):
        saved, setup.MIRROR = setup.MIRROR, f"http://127.0.0.1:{free_port()}/zim"
        try:
            with self.assertRaises(setup.Stop) as cm:
                self.rows()
            self.assertIn("internet connection", str(cm.exception))
        finally:
            setup.MIRROR = saved

    def test_nothing_found_is_an_error_not_success(self):
        fakes.FakeKiwix.files = {}
        with self.assertRaises(setup.Stop):
            self.rows()

    # ---- downloading
    def test_download(self):
        r = self.row("wikipedia_en_all_mini")
        with quiet_fd():
            self.assertTrue(setup.download(r, 1, 1))
        self.assertEqual((self.lib / r["file"]).read_bytes(), fakes.FakeKiwix.files["wikipedia/" + r["file"]])
        self.assertEqual(self.names(), [r["file"]])
        fakes.FakeKiwix.hits.clear()
        self.assertTrue(setup.download(r, 1, 1))  # second time: nothing to do
        self.assertEqual(fakes.FakeKiwix.hits, [])

    def test_download_resumes(self):
        r = self.row("wikipedia_en_all_nopic")
        data = fakes.FakeKiwix.files["wikipedia/" + r["file"]]
        (self.lib / (r["file"] + ".part")).write_bytes(data[:400_000])
        with quiet_fd():
            self.assertTrue(setup.download(r, 1, 1))
        self.assertEqual((self.lib / r["file"]).read_bytes(), data)
        ranges = [rng for path, rng in fakes.FakeKiwix.hits if path.endswith(".zim")]
        self.assertEqual(ranges, ["bytes=400000-"], "it should have continued, not started over")

    def test_download_complete_but_not_yet_checked(self):
        r = self.row("wikipedia_en_all_mini")
        data = fakes.FakeKiwix.files["wikipedia/" + r["file"]]
        (self.lib / (r["file"] + ".part")).write_bytes(data)
        self.assertTrue(setup.download(r, 1, 1))
        self.assertFalse([p for p, _ in fakes.FakeKiwix.hits if p.endswith(".zim")])

    def test_damaged_download_is_fetched_again(self):
        r = self.row("wikipedia_en_all_mini")
        fakes.FakeKiwix.corrupt_once.add("wikipedia/" + r["file"])
        with quiet_fd():
            self.assertTrue(setup.download(r, 1, 1))
        self.assertEqual((self.lib / r["file"]).read_bytes(), fakes.FakeKiwix.files["wikipedia/" + r["file"]])
        self.assertIn("damaged", self.out.getvalue())

    def test_damaged_partial_file_is_fetched_again(self):
        r = self.row("wikipedia_en_all_mini")
        (self.lib / (r["file"] + ".part")).write_bytes(b"junk" * 100_000)  # bigger than the real file
        with quiet_fd():
            self.assertTrue(setup.download(r, 1, 1))
        self.assertEqual(self.names(), [r["file"]])

    def test_download_falls_back_from_a_dead_mirror(self):
        r = self.row("wikipedia_en_all_mini")
        with quiet_fd():
            self.assertTrue(setup.download(r, 1, 1, mirror=setup.MIRRORS[1]))

    def test_download_failure_is_reported_not_fatal(self):
        r = self.row("wikipedia_en_all_mini")
        fakes.FakeKiwix.fail_meta.add("wikipedia/" + r["file"])
        self.assertFalse(setup.download(r, 1, 1))
        del fakes.FakeKiwix.fail_meta
        fakes.FakeKiwix.fail_meta = set()
        data = fakes.FakeKiwix.files.pop("wikipedia/" + r["file"])
        try:
            with quiet_fd():
                saved = setup.meta4
                setup.meta4 = lambda row: (len(data), hashlib.sha256(data).hexdigest())
                self.assertFalse(setup.download(r, 1, 1))  # the file itself is missing on every server
        finally:
            setup.meta4 = saved
        self.assertFalse((self.lib / r["file"]).exists())

    def test_download_refuses_to_fill_the_disk(self):
        r = self.row("wikipedia_en_all_nopic")
        shutil.disk_usage = lambda p: self._usage(p)._replace(free=setup.FLOOR + 1000)
        self.assertFalse(setup.download(r, 1, 1))
        self.assertIn("not enough free space", self.out.getvalue())
        self.assertFalse([p for p, _ in fakes.FakeKiwix.hits if p.endswith(".zim")])

    def test_update_replaces_the_old_version(self):
        old = self.lib / "wikipedia_en_all_mini_2025-11.zim"
        old.write_bytes(b"old")
        (self.lib / "wikipedia_en_all_mini_2025-12.zim.part").write_bytes(b"old partial")
        (self.lib / "wikipedia_en_all_nopic_2025-11.zim").write_bytes(b"a different collection")
        (self.lib / "my_own_file.zim").write_bytes(b"mine")
        r = self.row("wikipedia_en_all_mini")
        with quiet_fd():
            self.assertTrue(setup.download(r, 1, 1))
        self.assertEqual(self.names(), ["my_own_file.zim", "wikipedia_en_all_mini_2026-02.zim",
                                        "wikipedia_en_all_nopic_2025-11.zim"])

    def test_old_version_is_kept_if_the_new_one_fails(self):
        old = self.lib / "wikipedia_en_all_mini_2025-11.zim"
        old.write_bytes(b"old")
        r = self.row("wikipedia_en_all_mini")
        fakes.FakeKiwix.fail_meta.add("wikipedia/" + r["file"])
        self.assertFalse(setup.download(r, 1, 1))
        self.assertTrue(old.exists())

    def test_remaining_bytes_counts_partial_downloads(self):
        rows, _ = self.rows()
        e = setup.tier_rows(rows, "essentials")
        total = sum(r["approx_size"] for r in e)
        self.assertEqual(setup.remaining_bytes(e), total)
        (self.lib / "wikipedia_en_all_mini_2026-02.zim.part").write_bytes(b"x" * 100_000)
        self.assertEqual(setup.remaining_bytes(e), total - 100_000)

    def test_fastest_mirror_skips_dead_ones(self):
        with quiet_fd():
            self.assertEqual(setup.fastest_mirror(self.row("wikipedia_en_all_nopic")), setup.MIRROR)

    # ---- the AI model
    def test_model_choice_by_memory(self):
        for gb, want in [(8, "bonsai-2:4b"), (16, "bonsai-2:9b"), (18, "bonsai-2:9b"), (24, "bonsai-2:9b"),
                         (32, "bonsai-2:27b"), (36, "bonsai-2:27b"), (64, "bonsai-2:27b")]:
            setup.ram_bytes = lambda gb=gb: gb * 1024**3
            self.assertEqual(setup.pick_model()[0], want, f"{gb} GB")
        setup.ram_bytes = lambda: 16 * 1024**3

    def test_model_pull(self):
        with quiet_fd():
            self.assertIsNone(setup.pull_model("bonsai-2:9b"))
        self.assertTrue(setup.model_present("bonsai-2:9b"))

    def test_model_pull_failure_is_a_message(self):
        with quiet_fd():
            error = setup.pull_model("missing-model:1b")
        self.assertIn("does not exist", error)
        saved, setup.OLLAMA_HOST = setup.OLLAMA_HOST, f"127.0.0.1:{free_port()}"
        try:
            with quiet_fd():
                self.assertTrue(setup.pull_model("bonsai-2:9b"))
        finally:
            setup.OLLAMA_HOST = saved

    # ---- whole runs
    def test_full_install_essentials(self):
        self.assertEqual(self.run_main("--tier", "essentials"), 0)
        self.assertEqual(self.names(), ["wikipedia_en_all_mini_2026-02.zim", "zimgit-test_en_2026-02.zim"])
        self.assertEqual(json.loads(setup.CONFIG.read_text()), {"tier": "essentials", "model": "bonsai-2:9b"})
        self.assertEqual(fakes.FakeOllama.pulled, ["bonsai-2:9b"])
        self.assertTrue((self.lib / ".pdfindex" / "zimgit-test_en_2026-02.v2.sqlite").exists())
        for launcher in (TMP / "home" / "Desktop" / "Offline Kit.command", KIT / "Offline Kit.command"):
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertIn(f'"{KIT}/kit/start.sh"', launcher.read_text())
        self.assertIn("All set!", self.out.getvalue())

    def test_rerun_downloads_nothing_new(self):
        self.run_main("--tier", "essentials")
        fakes.FakeKiwix.hits.clear()
        self.assertEqual(self.run_main(), 0)  # the size is remembered
        self.assertFalse([p for p, _ in fakes.FakeKiwix.hits if p.endswith(".zim")])
        self.assertEqual(self.names(), ["wikipedia_en_all_mini_2026-02.zim", "zimgit-test_en_2026-02.zim"])

    def test_changing_size_swaps_collections(self):
        self.run_main("--tier", "essentials")
        (self.lib / "my_own_file.zim").write_bytes(b"mine")
        # 1 = finished with something to report: "Gone" is no longer on the server.
        self.assertEqual(self.run_main("--tier", "standard"), 1)
        self.assertEqual(self.names(), ["my_own_file.zim", "wikipedia_en_all_nopic_2026-01.zim",
                                        "zimgit-test_en_2026-02.zim"])
        self.assertIn("couldn't be found on the Kiwix server and were left out: Gone", self.out.getvalue())
        self.assertEqual(self.run_main("--tier", "essentials"), 0)
        self.assertEqual(self.names(), ["my_own_file.zim", "wikipedia_en_all_mini_2026-02.zim",
                                        "zimgit-test_en_2026-02.zim"])

    def test_app_only_update_leaves_the_library_alone(self):
        self.run_main("--tier", "essentials")
        old = self.lib / "wikipedia_en_all_mini_2026-02.zim"
        # A newer version is out, the server is unreachable, and a collection from another size is present.
        fakes.FakeKiwix.files["wikipedia/wikipedia_en_all_mini_2026-09.zim"] = blob("newer", 1000)
        (self.lib / "wikipedia_en_all_nopic_2026-01.zim").write_bytes(b"other size")
        before = self.names()
        fakes.FakeKiwix.hits.clear()
        fakes.FakeOllama.pulled.clear()
        saved, setup.MIRROR = setup.MIRROR, f"http://127.0.0.1:{free_port()}/zim"
        try:
            self.assertEqual(self.run_main("--app-only"), 0)
        finally:
            setup.MIRROR = saved
        self.assertEqual(fakes.FakeKiwix.hits, [])
        self.assertEqual(fakes.FakeOllama.pulled, [], "the model is already there")
        self.assertEqual(self.names(), before)
        self.assertTrue(old.exists())
        self.assertIn("has been updated", self.out.getvalue())
        self.assertTrue(os.access(KIT / "Update Offline Kit.command", os.X_OK))
        subprocess.run(["bash", "-n", str(KIT / "Update Offline Kit.command")], check=True)

    def test_app_only_on_a_new_mac_is_a_normal_install(self):
        self.assertEqual(self.run_main("--app-only", "--tier", "essentials"), 0)
        self.assertEqual(self.names(), ["wikipedia_en_all_mini_2026-02.zim", "zimgit-test_en_2026-02.zim"])

    def test_nothing_is_removed_when_a_download_fails(self):
        self.run_main("--tier", "essentials")
        fakes.FakeKiwix.fail_meta.add("wikipedia/wikipedia_en_all_nopic_2026-01.zim")
        self.assertEqual(self.run_main("--tier", "standard"), 1)
        self.assertIn("wikipedia_en_all_mini_2026-02.zim", self.names())
        self.assertIn("didn't finish downloading: Wikipedia (full text)", self.out.getvalue())

    def test_model_problem_does_not_stop_the_library(self):
        self.assertEqual(self.run_main("--tier", "essentials", "--model", "missing-model:1b"), 1)
        self.assertEqual(self.names(), ["wikipedia_en_all_mini_2026-02.zim", "zimgit-test_en_2026-02.zim"])
        self.assertIn("couldn't be downloaded", self.out.getvalue())
        self.assertTrue((TMP / "home" / "Desktop" / "Offline Kit.command").exists())

    def test_not_enough_space_stops_before_downloading(self):
        shutil.disk_usage = lambda p: self._usage(p)._replace(free=1000)
        with self.assertRaises(setup.Stop) as cm:
            self.run_main("--tier", "standard")
        self.assertIn("not enough free space", str(cm.exception))
        self.assertEqual(self.names(), [])

    def test_size_question(self):
        asked = []
        saved, setup.ask = setup.ask, lambda prompt: asked.append(prompt) or "1"
        try:
            self.assertEqual(self.run_main(), 0)
        finally:
            setup.ask = saved
        self.assertEqual(json.loads(setup.CONFIG.read_text())["tier"], "essentials")
        self.assertIn("[3]", asked[0])  # with plenty of space, the biggest size is suggested
        setup.CONFIG.unlink()
        saved, setup.ask = setup.ask, lambda prompt: "banana"
        shutil.disk_usage = lambda p: self._usage(p)._replace(free=setup.RESERVE + 3_000_000_000)
        try:
            self.run_main()
        finally:
            setup.ask = saved
        self.assertIn("didn't understand", self.out.getvalue())


# ====================================================================== start.sh


STUB_OLLAMA = "#!/bin/bash\n# stand-in AI engine for the tests\nwhile :; do sleep 0.2; done\n"


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl") and shutil.which("pkill"), "needs bash, curl, pkill")
class LauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kit = TMP / "Launch Kit"  # a space in the path, like "Macintosh HD" or a user name
        (cls.kit / "library").mkdir(parents=True)
        shutil.copytree(REPO, cls.kit / "kit", ignore=shutil.ignore_patterns(".git", "__pycache__"))
        (cls.kit / "venv" / "bin").mkdir(parents=True)
        py = cls.kit / "venv" / "bin" / "python"
        py.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
        (cls.kit / "ollama").mkdir()
        (cls.kit / "ollama" / "ollama").write_text(STUB_OLLAMA)
        cls.stubs = TMP / "stubs"
        cls.stubs.mkdir()
        (cls.stubs / "open").write_text(f'#!/bin/bash\necho "$@" >> "{TMP}/opened"\n')
        (cls.stubs / "clear").write_text("#!/bin/bash\n")
        for f in (py, cls.kit / "ollama" / "ollama", cls.stubs / "open", cls.stubs / "clear"):
            f.chmod(0o755)

    def start(self, port):
        env = dict(os.environ, PATH=f"{self.stubs}:{os.environ['PATH']}", OFFLINE_KIT_PORT=str(port))
        return subprocess.Popen(["bash", str(self.kit / "kit" / "start.sh")], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def engine_pids(self):
        out = subprocess.run(["pgrep", "-f", f"{self.kit}/ollama/"], capture_output=True, text=True).stdout
        return out.split()

    def wait_up(self, port, proc):
        for _ in range(150):
            try:
                if get(f"http://127.0.0.1:{port}", "/api/status")[0] == 200:
                    return
            except OSError:
                pass
            self.assertIsNone(proc.poll(), "start.sh exited early")
            time.sleep(0.2)
        self.fail("the app never came up")

    def test_start_twice_then_stop(self):
        try:
            urllib.request.urlopen("http://127.0.0.1:11435/api/version", timeout=2)
            self.skipTest("Offline Kit is running; close it to run this test")
        except OSError:
            pass
        port = free_port()
        opened = TMP / "opened"
        opened.unlink(missing_ok=True)
        first = self.start(port)
        try:
            self.wait_up(port, first)
            for _ in range(50):
                if opened.exists():
                    break
                time.sleep(0.1)
            self.assertEqual(opened.read_text().split(), [f"http://127.0.0.1:{port}/"])
            self.assertEqual(len(self.engine_pids()), 1)

            # Double-clicking the icon again must not disturb the copy that's running.
            second = self.start(port)
            out, _ = second.communicate(timeout=30)
            self.assertEqual(second.returncode, 0)
            self.assertIn("already running", out)
            self.assertEqual(len(opened.read_text().split()), 2)
            self.assertIsNone(first.poll())
            self.assertEqual(len(self.engine_pids()), 1, "the second start stopped the AI engine")
            self.assertEqual(get(f"http://127.0.0.1:{port}", "/api/status")[0], 200)

            # Closing the Terminal window sends a hang-up signal.
            os.kill(first.pid, signal.SIGHUP)
            out, _ = first.communicate(timeout=30)
            self.assertEqual(out.count("Stopping Offline Kit"), 1)
            self.assertEqual(self.engine_pids(), [], "the AI engine was left running")
            with self.assertRaises(OSError):
                get(f"http://127.0.0.1:{port}", "/api/status")
        finally:
            for p in (first,):
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
            subprocess.run(["pkill", "-f", f"{self.kit}/ollama/"])

    def test_port_taken_by_something_else(self):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            proc = self.start(blocker.getsockname()[1])
            out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not start", out)
        self.assertIn("couldn't start on port", out)  # the reason, from the log
        self.assertEqual(self.engine_pids(), [])

    def test_incomplete_install(self):
        moved = self.kit / "ollama.away"
        (self.kit / "ollama").rename(moved)
        try:
            proc = self.start(free_port())
            out, _ = proc.communicate(timeout=30)
        finally:
            moved.rename(self.kit / "ollama")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not completely installed", out)

    def test_scripts_parse(self):
        for script in ("install.sh", "start.sh"):
            subprocess.run(["bash", "-n", str(REPO / script)], check=True)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1, exit=False)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
