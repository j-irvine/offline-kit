# Offline Kit

A library and an AI assistant that work on your Mac **with no internet connection**.

- **Library:** all of Wikipedia, medical references (including *Where There Is No Doctor*), repair guides, survival and first-aid manuals, food preservation, gardening, off-grid energy, and more.
- **Ask:** a ChatGPT-style assistant that runs entirely on your laptop. It looks up the answer in the library and shows you which articles it used.

It's meant for when the internet is down for a long time. Set it up now, while you still have internet.

---

## What you need

- A Mac with an Apple processor (M1, M2, M3, M4 or later). [How to check](https://support.apple.com/en-us/116943)
  - Older Intel Macs work, but the assistant will be very slow.
- **16 GB of memory** is recommended. 8 GB works, with a smaller and less capable assistant.
- Free disk space for the size you choose:

| Size | Space needed | What's included |
|---|---|---|
| **Essentials** | about 30 GB | Wikipedia summaries, medical references, repair guides, survival guides, practical Q&A |
| **Standard** | about 80 GB | Full Wikipedia text, plus 30+ practical collections |
| **Complete** | about 200 GB | Everything, including Wikipedia with all its images |

The installer checks your free space and recommends a size that fits.

## Install

1. **Get access.** This repository is private. Make a free account at [github.com](https://github.com/signup), send your username to whoever shared this, and accept the invitation email from GitHub.
2. **Download the kit.** On this page, click the green **Code** button, then **Download ZIP**. If your Downloads folder shows a `.zip` file instead of a folder, double-click it to unzip it. You should end up with a folder called `offline-kit-main` in Downloads.
3. **Open Terminal.** Press <kbd>⌘ Command</kbd> + <kbd>Space</kbd>, type `Terminal`, and press <kbd>Return</kbd>.
4. **Run the installer.** Copy this line, paste it into Terminal, and press <kbd>Return</kbd>:

   ```
   bash ~/Downloads/offline-kit-main/install.sh
   ```

   If macOS asks whether Terminal can access your Downloads or Desktop folder, click **Allow**.
5. When asked, type the number of the size you want and press <kbd>Return</kbd>.
6. **Wait.** The download is large and can take a few hours. You can keep using your Mac, but keep it plugged in and leave the Terminal window open.
   - **If it gets interrupted** (Wi-Fi drops, the Mac restarts, you close the window), paste the line from step 4 again. It continues where it left off.
7. When it says **All set!**, you're done. You can delete the `offline-kit-main` folder and the ZIP from Downloads.

## Use it

Double-click **Offline Kit** on your Desktop. A Terminal window opens, then the app opens in your web browser.

- **Ask** tab: type a question the way you'd search Google. Answers include numbered links to the articles they came from. Click them to read the source.
- **Library** tab: search and read everything directly, like a website.
- **Keep the Terminal window open** while you use it. Close that window when you're done.

**Try it now with Wi-Fi turned off,** so you know it works before you need it.

> ⚠️ The AI can be confidently wrong. For medical, safety or other important questions, click through and read the source articles. The answers point you to them for exactly this reason.

## Tips for when the internet is down

- **Power matters most.** Running the assistant uses a lot of battery. A solar panel or a large USB-C power bank that can charge a laptop goes a long way. Reading the library uses much less battery than asking the AI.
- **Put the essentials on your phone too.** Install the free **Kiwix** app (iPhone and Android) and download *WikiMed* and a Wikipedia edition in it. Install **Organic Maps** and download maps of your area.
- **Print the most critical things,** like first aid and water purification, in case all your devices are dead.

## Update, change size, or remove

- **Update** (about once a year): download the ZIP again (step 2) and paste the install line again (step 4). It downloads only what's new and removes the old versions.
- **Change size:** paste this into Terminal, with the size you want at the end (`essentials`, `standard` or `complete`):
  ```
  bash ~/OfflineKit/kit/install.sh --tier essentials
  ```
- **Remove:** drag the **OfflineKit** folder in your home folder and the **Offline Kit** Desktop shortcut to the Trash. Nothing else was changed on your Mac.

## Troubleshooting

- **"Not enough space":** free up space (Apple menu → System Settings → General → Storage), then run the installer again. You can also choose a smaller size.
- **The page says the AI engine isn't running:** close the Offline Kit Terminal window and double-click Offline Kit again.
- **Answers are slow:** the first question after starting takes longer while the assistant loads. Slower answers are normal on 8 GB Macs. Turning off **Think longer** makes answers faster.
- **Something else:** the logs are `ollama.log` and `server.log` in the OfflineKit folder in your home folder. Send them to whoever shared this with you.

---

## How it works (for the curious)

- **Library:** [Kiwix](https://kiwix.org) `.zim` files, the same offline Wikipedia format used by schools and research stations around the world. The installer always fetches the newest version of each collection listed in [`catalog.tsv`](catalog.tsv). Edit that file to add or remove collections.
- **Search:** the full-text index built into each `.zim` file, read with [libzim](https://github.com/openzim/python-libzim). Some collections are folders of PDFs, which that index doesn't cover, so the app extracts their text into a small SQLite index (`app/pdfindex.py`).
- **AI:** an open-weights model run by [Ollama](https://ollama.com), chosen to fit your Mac's memory (see [`docs/MODELS.md`](docs/MODELS.md)).
- **Answering a question:** the model first turns your question into search queries. The app searches the whole library, picks the most relevant paragraphs, and the model answers from those paragraphs with citations (`app/server.py`).
- **Self-contained:** everything lives in `~/OfflineKit`: a private copy of Python (via [uv](https://github.com/astral-sh/uv)), Ollama, the model and the library. Nothing is installed system-wide, and nothing runs in the background when the app is closed.
- **Private:** the app only listens on your own computer (`127.0.0.1`) and never contacts the internet.

```
~/OfflineKit/
  kit/           this repository
  library/       .zim collections (+ .pdfindex/)
  models/        AI model files
  ollama/  python/  venv/  bin/   runtimes
  config.json    chosen size and model
```
