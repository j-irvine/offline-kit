// Offline Kit web app: chat with the offline AI and browse the offline library.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ------------------------------------------------------------------ storage (per browser)
const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
};

// ------------------------------------------------------------------ tabs
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "library") { loadBooks(); $("#search-q").focus(); } else { $("#question").focus(); }
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

// ------------------------------------------------------------------ status
async function refreshStatus() {
  const el = $("#status"), text = $("#status-text");
  try {
    const s = await (await fetch("/api/status")).json();
    if (!s.ollama) { el.className = "status bad"; text.textContent = "AI engine not running"; }
    else if (!s.modelReady) { el.className = "status bad"; text.textContent = `Model ${s.model} not installed`; }
    else { el.className = "status ok"; text.textContent = `${s.books} collections · ${s.model}`; }
    el.title = text.textContent;
  } catch { el.className = "status bad"; text.textContent = "Offline Kit server stopped"; }
}
refreshStatus();
setInterval(refreshStatus, 15000);

// ------------------------------------------------------------------ markdown (small, safe subset)
function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((\/content\/[^)\s]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  return s;
}
function markdown(src) {
  const lines = src.replace(/\r/g, "").split("\n");
  let out = "", i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out += `<pre><code>${esc(buf.join("\n"))}</code></pre>`; continue;
    }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)/))) { const n = Math.min(m[1].length + 1, 4); out += `<h${n}>${inline(m[2])}</h${n}>`; i++; continue; }
    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line); const items = [];
      while (i < lines.length && (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        if (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) items.push(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
        else items[items.length - 1] += " " + lines[i].trim();
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out += `<${tag}>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</${tag}>`; continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const row = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = row(line); i += 2; const body = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
      out += `<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*[-*+]\s|\s*\d+[.)]\s|\s*\|)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) buf.push(lines[i++]);
    out += `<p>${buf.map(inline).join("<br>")}</p>`;
  }
  return out;
}
function renderAnswer(text, sources) {
  let htmlText = markdown(text);
  const byN = Object.fromEntries((sources || []).map((s) => [s.n, s]));
  // Turn [1] or [1, 2] into links to the source articles.
  htmlText = htmlText.replace(/\[(\d+(?:\s*,\s*\d+)*)\](?!\()/g, (all, nums) =>
    nums.split(/\s*,\s*/).map((n) => byN[n]
      ? `<a class="cite" href="${byN[n].url}" target="_blank" title="${esc(byN[n].title)} — ${esc(byN[n].bookTitle)}">${n}</a>`
      : `[${n}]`).join(""));
  return htmlText;
}

// ------------------------------------------------------------------ chats
let chats = store.get("chats", []);   // [{id, title, messages:[{role, content, sources, stats}]}]
let current = null;
let controller = null;

function saveChats() { store.set("chats", chats.slice(0, 100)); }
function newChat() {
  current = null;
  $("#thread").innerHTML = "";
  $("#thread").appendChild(welcomeEl);
  renderHistory();
  $("#question").focus();
}
function openChat(id) {
  current = chats.find((c) => c.id === id) || null;
  if (!current) return newChat();
  $("#thread").innerHTML = "";
  current.messages.forEach((m) => addMessage(m));
  renderHistory();
  scrollDown(true);
}
function renderHistory() {
  const ul = $("#history-list");
  ul.innerHTML = "";
  chats.forEach((c) => {
    const li = document.createElement("li");
    if (current && c.id === current.id) li.className = "current";
    li.innerHTML = `<button class="open" title="${esc(c.title)}">${esc(c.title)}</button><button class="del" title="Delete">×</button>`;
    li.querySelector(".open").onclick = () => openChat(c.id);
    li.querySelector(".del").onclick = () => {
      chats = chats.filter((x) => x.id !== c.id); saveChats();
      if (current && current.id === c.id) newChat(); else renderHistory();
    };
    ul.appendChild(li);
  });
}

const welcomeEl = $("#welcome");
function scrollDown(force) {
  const t = $("#thread");
  if (force || t.scrollHeight - t.scrollTop - t.clientHeight < 160) t.scrollTop = t.scrollHeight;
}

function addMessage(m) {
  welcomeEl.remove();
  const el = document.createElement("div");
  el.className = "msg " + m.role;
  if (m.role === "user") {
    el.innerHTML = `<div class="bubble">${esc(m.content)}</div>`;
  } else {
    el.innerHTML = `<div class="meta"></div><details class="thinking" hidden><summary>Reasoning</summary><div></div></details><div class="answer"></div><div class="sources"></div><div class="note"></div>`;
    fillAssistant(el, m);
  }
  $("#thread").appendChild(el);
  return el;
}
function fillAssistant(el, m) {
  $(".answer", el).innerHTML = m.error ? `<p class="error">${esc(m.error)}</p>` : renderAnswer(m.content || "", m.sources);
  if (m.thinking) { $(".thinking", el).hidden = false; $(".thinking div", el).textContent = m.thinking; }
  const src = (m.sources || []).map((s) =>
    `<a class="source" href="${s.url}" target="_blank"><b>${s.n}</b> ${esc(s.title)} <span>${esc(s.bookTitle)}</span></a>`).join("");
  $(".sources", el).innerHTML = src;
  const notes = [];
  if (m.usedLibrary && m.sources && !m.sources.length && m.content) notes.push("Nothing relevant was found in the library, so this answer is from the AI's own knowledge.");
  if (m.stopped) notes.push("Stopped.");
  $(".note", el).textContent = notes.join(" ");
}

async function ask(question) {
  question = question.trim();
  if (!question || controller) return;
  if (!current) {
    current = { id: Date.now().toString(36), title: question.slice(0, 60), messages: [] };
    chats.unshift(current);
  } else {
    chats = [current, ...chats.filter((c) => c.id !== current.id)];
  }
  const history = current.messages.filter((m) => !m.error).map((m) => ({ role: m.role, content: m.content }));
  const userMsg = { role: "user", content: question };
  current.messages.push(userMsg);
  addMessage(userMsg);
  const useLibrary = $("#use-library").checked;
  const reply = { role: "assistant", content: "", sources: [], thinking: "", usedLibrary: useLibrary };
  current.messages.push(reply);
  const el = addMessage(reply);
  const meta = $(".meta", el);
  const setStatus = (t) => { meta.innerHTML = t ? `<span class="spinner"></span>${esc(t)}` : ""; };
  setStatus("Starting…");
  renderHistory();
  scrollDown(true);

  controller = new AbortController();
  $("#send").hidden = true; $("#stop").hidden = false;
  let pending = false;
  const redraw = () => { if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; fillAssistant(el, reply); scrollDown(); }); } };
  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
      body: JSON.stringify({ messages: [...history, { role: "user", content: question }], useLibrary, think: $("#think").checked }),
    });
    if (!res.ok) throw new Error(await res.text());
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.type === "status") setStatus(ev.text);
        else if (ev.type === "sources") { reply.sources = ev.sources; reply.queries = ev.queries; redraw(); }
        else if (ev.type === "thinking") { reply.thinking += ev.text; setStatus("Thinking…"); redraw(); }
        else if (ev.type === "token") { if (!reply.content) setStatus("Writing…"); reply.content += ev.text; redraw(); }
        else if (ev.type === "error") { reply.error = ev.text; redraw(); }
        else if (ev.type === "done") { reply.stats = ev.stats; }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") reply.stopped = true;
    else reply.error = "Couldn't reach Offline Kit. Is the Offline Kit window still open? " + (e.message || "");
  } finally {
    controller = null;
    $("#send").hidden = false; $("#stop").hidden = true;
    const s = reply.stats;
    meta.textContent = s && s.tokens && s.seconds ? `${Math.round(s.tokens / s.seconds)} words/sec` : "";
    fillAssistant(el, reply);
    saveChats();
    scrollDown();
  }
}

const q = $("#question");
function autosize() { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, 200) + "px"; }
q.addEventListener("input", autosize);
q.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#ask-form").requestSubmit(); }
});
$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); const v = q.value; q.value = ""; autosize(); ask(v); });
$("#stop").addEventListener("click", () => controller && controller.abort());
$("#new-chat").addEventListener("click", newChat);
document.querySelectorAll(".example").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
$("#use-library").checked = store.get("useLibrary", true);
$("#think").checked = store.get("think", false);
$("#use-library").addEventListener("change", (e) => store.set("useLibrary", e.target.checked));
$("#think").addEventListener("change", (e) => store.set("think", e.target.checked));
renderHistory();

// ------------------------------------------------------------------ library
let booksLoaded = false;
const fmtNum = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + " million" : n >= 1e3 ? Math.round(n / 1e3) + ",000" : String(n);

async function loadBooks() {
  if (booksLoaded) return;
  booksLoaded = true;
  const books = await (await fetch("/api/books")).json();
  const sel = $("#search-book");
  books.forEach((b) => { const o = document.createElement("option"); o.value = b.id; o.textContent = b.title; sel.appendChild(o); });
  const want = new URLSearchParams(location.search).get("book");
  if (want) sel.value = want;
  $("#books").innerHTML = books.length ? books.map((b) => `
    <a class="book" href="/content/${encodeURIComponent(b.id)}/" target="_blank">
      <img src="/api/illustration?book=${encodeURIComponent(b.id)}" alt="" onerror="this.style.visibility='hidden'">
      <div><h3>${esc(b.title)}</h3><p>${esc(b.description)}</p><small>${fmtNum(b.articles)} articles</small></div>
    </a>`).join("") : `<p class="empty">No collections are installed yet. Run the installer to download the library.</p>`;
}

async function search(qs, book, start = 0) {
  const box = $("#search-results");
  $("#suggestions").hidden = true;
  if (!qs.trim()) { box.innerHTML = ""; return; }
  if (!start) box.innerHTML = `<p class="empty">Searching…</p>`;
  const data = await (await fetch(`/api/search?q=${encodeURIComponent(qs)}&book=${encodeURIComponent(book)}&start=${start}`)).json();
  const html = data.results.map((r) => `
    <div class="result">
      <a href="/content/${encodeURIComponent(r.book)}/${r.path.split("/").map(encodeURIComponent).join("/")}" target="_blank">${esc(r.title)}</a>
      <div class="from">${esc(r.bookTitle)}</div>
      ${r.snippet ? `<p>${esc(r.snippet)}</p>` : ""}
    </div>`).join("");
  const pdfHtml = (data.pdfs || []).map((r) => `
    <div class="result">
      <a href="${r.url}" target="_blank">${esc(r.title)}</a>
      <div class="from">PDF guide · ${esc(r.bookTitle)} · page ${r.page}</div>
      <p>${esc(r.snippet)}</p>
    </div>`).join("");
  if (!start) {
    box.innerHTML = (pdfHtml ? `<h2 class="section-title">In PDF guides</h2>${pdfHtml}<h2 class="section-title">Articles</h2>` : "") +
      (data.results.length ? `<p class="from">About ${fmtNum(data.total)} results</p>` + html : (pdfHtml ? "" : `<p class="empty">No results. Try different words.</p>`));
  }
  else { $(".more", box)?.remove(); box.insertAdjacentHTML("beforeend", html); }
  if (start + data.results.length < data.total && data.results.length) {
    box.insertAdjacentHTML("beforeend", `<button class="btn more">More results</button>`);
    $(".more", box).onclick = () => search(qs, book, start + data.results.length);
  }
}
$("#search-form").addEventListener("submit", (e) => { e.preventDefault(); search($("#search-q").value, $("#search-book").value); });

let suggestTimer;
$("#search-q").addEventListener("input", (e) => {
  clearTimeout(suggestTimer);
  const v = e.target.value.trim();
  if (v.length < 2) { $("#suggestions").hidden = true; return; }
  suggestTimer = setTimeout(async () => {
    const items = await (await fetch("/api/suggest?q=" + encodeURIComponent(v))).json();
    const ul = $("#suggestions");
    ul.innerHTML = items.map((s) => `<li><a href="/content/${encodeURIComponent(s.book)}/${s.path.split("/").map(encodeURIComponent).join("/")}" target="_blank">${esc(s.title)}<span>${esc(s.bookTitle)}</span></a></li>`).join("");
    ul.hidden = !items.length;
  }, 200);
});

// Links from the toolbar on library pages: /?tab=library&q=...&book=...
const params = new URLSearchParams(location.search);
if (params.get("tab") === "library") {
  showTab("library");
  loadBooks().then(() => {
    if (params.get("q")) { $("#search-q").value = params.get("q"); search(params.get("q"), params.get("book") || ""); }
  });
}
