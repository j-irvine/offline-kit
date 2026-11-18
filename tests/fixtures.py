"""Builds small .zim files for the tests (run by run_tests.py)."""

import base64
from pathlib import Path

from libzim.writer import Creator, Hint, Item, StringProvider

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class _Item(Item):
    def __init__(self, path, title, content, mimetype="text/html", front=True):
        super().__init__()
        self._p, self._t, self._c, self._m, self._f = path, title, content, mimetype, front

    def get_path(self):
        return self._p

    def get_title(self):
        return self._t

    def get_mimetype(self):
        return self._m

    def get_contentprovider(self):
        return StringProvider(self._c)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: self._f and self._m == "text/html"}


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title></head>"
            f"<body class='x'><h1>{title}</h1>{body}</body></html>")


def make_pdf(pages, title=None):
    """A minimal text PDF: `pages` is a list of lists of lines."""
    objs = []

    def add(body):
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids, pages_id = [], len(objs) + 2 * len(pages) + 1
    for lines in pages:
        text = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(
            "(%s) Tj T*" % l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for l in lines) + " ET"
        stream = text.encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (pages_id, content, font)))
    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    trailer = b"/Root %d 0 R" % catalog
    if title:
        info = add(b"<< /Title (%s) >>" % title.encode("latin-1"))
        trailer += b" /Info %d 0 R" % info
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d %s >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, trailer, xref)
    return bytes(out)


WATER = page("Water purification", """
<p>Water purification is the process of removing undesirable chemicals, biological contaminants, suspended solids,
and gases from water<sup class="reference">[1]</sup>. The goal is to produce water fit for drinking.</p>
<h2>Boiling</h2>
<p>Boiling is the most reliable way to make stream water safe. Bring clear water to a rolling boil for one minute.
At altitudes above 2000 metres, boil water for three minutes. Let the water cool before drinking.</p>
<h2>Chemical disinfection</h2>
<p>Unscented household bleach can disinfect drinking water. Add 2 drops of 5 to 9 percent bleach per litre of clear
water, stir, and wait 30 minutes before drinking.</p>
<table><tr><th>Volume</th><th>Bleach</th></tr><tr><td>1 litre</td><td>2 drops</td></tr></table>
<div class="navbox">NAVBOXJUNK purification portal</div>
<script>var SCRIPTJUNK = "purification";</script>
<h2>References</h2>
<p>REFERENCEJUNK about water purification citations.</p>
""")

HYPOTHERMIA = page("Hypothermia", """
<p>Hypothermia is a body core temperature below 35 degrees Celsius. Signs include shivering, confusion,
slurred speech and drowsiness.</p>
<h2>Treatment</h2>
<p>Move the person out of the cold, remove wet clothing, and warm the trunk of the body first with blankets.
Give warm sweet drinks if the person is awake. Severe hypothermia needs urgent medical help.</p>
""")


def build_wiki(path, extra_marker=""):
    with Creator(str(path)).config_indexing(True, "eng") as c:
        c.set_mainpath("Main_Page")
        for key, val in [("Title", "Test Wikipedia"), ("Description", "A tiny test encyclopedia"),
                         ("Language", "eng"), ("Name", "testwiki"), ("Creator", "tests"), ("Publisher", "tests"),
                         ("Date", "2026-01-01")]:
            c.add_metadata(key, val)
        c.add_illustration(PNG, 48)
        c.add_item(_Item("Main_Page", "Main Page", page("Main Page", f"<p>Welcome {extra_marker}. "
                         "<a href='Water_purification'>Water</a> <img src='img/dot.png'></p>")))
        c.add_item(_Item("Water_purification", "Water purification", WATER))
        c.add_item(_Item("Hypothermia", "Hypothermia", HYPOTHERMIA))
        c.add_item(_Item("Café_(drink)", "Café (drink)", page("Café (drink)",
                         "<p>Coffee is a brewed drink prepared from roasted espresso beans.</p>")))
        c.add_item(_Item("AC/DC", "AC/DC", page("AC/DC", "<p>An Australian rock band with electric guitars.</p>")))
        c.add_item(_Item("What_is_100%_juice?", "What is 100% juice?", page("What is 100% juice?",
                         "<p>Juice squeezed from oranges with nothing added.</p>")))
        c.add_item(_Item("users/123", "User 123", page("User 123",
                         "<p>Profile of a user who likes water purification boiling bleach.</p>")))
        c.add_item(_Item("NoBody", "No body tag", "<h1>Fragment</h1><p>A zebra fragment without a body tag.</p>"))
        c.add_item(_Item("img/dot.png", "dot", PNG, "image/png"))
        c.add_item(_Item("style.css", "style", "body { color: black }", "text/css"))
        c.add_redirection("Purifying_water", "Purifying water", "Water_purification", {Hint.FRONT_ARTICLE: True})
        c.add_redirection("Loop_A", "Loop A", "Loop_B", {})
        c.add_redirection("Loop_B", "Loop B", "Loop_A", {})


def build_guides(path):
    """A 'zimgit'-style collection: mostly PDFs."""
    with Creator(str(path)).config_indexing(True, "eng") as c:
        c.set_mainpath("index.html")
        for key, val in [("Title", "Test Guides"), ("Description", "PDF survival guides"), ("Language", "eng"),
                         ("Name", "zimgit-test"), ("Creator", "tests"), ("Publisher", "tests"), ("Date", "2026-02-01")]:
            c.add_metadata(key, val)
        c.add_item(_Item("index.html", "Guides", page("Guides", "<p><a href='files/knots guide.pdf'>Knots</a></p>")))
        c.add_item(_Item("files/knots guide.pdf", "knots guide.pdf", make_pdf([
            ["Essential Knots Field Guide", "A practical handbook for rope work in camp."],
            ["The bowline makes a fixed loop that will not slip under load.",
             "Form a small loop, pass the working end up through it, around the standing part,",
             "and back down through the small loop. Tighten firmly before trusting it."],
            ["The clove hitch fastens a rope to a post quickly. Wrap twice and tuck under."],
        ], title="Microsoft Word - knots.docx"), "application/pdf"))
        c.add_item(_Item("files/latrine.pdf", "latrine.pdf", make_pdf([
            ["Emergency sanitation: building a pit latrine at least 30 metres from any well or stream,",
             "downhill from the water source, and covering waste with soil or ash after each use."],
        ], title="Emergency Sanitation Guide"), "application/pdf"))
        c.add_item(_Item("files/broken.pdf", "broken.pdf", b"%PDF-1.4 this is not really a pdf", "application/pdf"))


def build_all(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    build_wiki(folder / "wikipedia_en_test_2026-01.zim")
    build_guides(folder / "zimgit-test_en_2026-02.zim")
    (folder / "damaged_en_all_2026-03.zim").write_bytes(b"this is not a zim file" * 100)
