# Choosing the AI model

The installer picks a model based on the Mac's memory (`MODEL_BY_RAM` in `app/setup.py`):

| Memory | Model | Download |
|---|---|---|
| 30 GB or more | `bonsai-2:27b` | 8.3 GB |
| 14–30 GB | `bonsai-2:9b` | 3.2 GB |
| Less than 14 GB | `bonsai-2:4b` | 1.6 GB |

The Bonsai 2 models use "ternary" weights. They're about half the size of ordinary 4-bit models of the same parameter count, which lets a reasonably capable model fit on a laptop.

## What testing showed (November 2026, M3 MacBook, 16 GB)

The test asked six practical questions (water purification, child dehydration, canning, allergic reactions, knots, and short-season vegetables) with library search turned on.

- **`bonsai-2:9b`:** about 25 words/sec, and a full answer in 30–45 seconds, including the library search. Answers were well organized and cited the right sources. It sometimes **mixes up numbers** that appear close together in a source. Asked about bleach for drinking water, it once gave the strength of a surface-cleaning solution from the same page. A prompt rule about checking that quantities match the situation helped, but it isn't reliable for exact figures.
- **`bonsai-2:27b`:** noticeably more accurate. It read the same bleach source correctly and pointed out where sources disagree. **But on a 16 GB Mac it doesn't fit comfortably:** the first word took more than 2 minutes and the whole Mac became sluggish. Only use it with 32 GB or more.

**Takeaway:** the assistant is a fast way to *find* the right information and summarize it. For anything where an exact number matters (doses, dilutions, temperatures, times), open the cited source and check.

## Using a different model

Any model from [ollama.com/library](https://ollama.com/library) works. To switch:

```
bash ~/OfflineKit/kit/install.sh --model qwen3.8:27b
```

That downloads the model and saves it in `~/OfflineKit/config.json`. Bigger models are smarter but slower. As a rule of thumb, the download size should be under about half the Mac's memory.
