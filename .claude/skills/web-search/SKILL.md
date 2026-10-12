---
name: web-search
description: "The private SearXNG container, the gateway's /v1/search and automatic search router, and the search guard (searchguard.py: cache, pacing, pause when engines refuse). Read before changing gateway.py's search code, searchguard.py or the agent's web_search handling, and before any test that searches the web."
---

# Web search: SearXNG, the router, the search guard

Part of the developer notes; the index is in `CLAUDE.md`.

## Web search

* **SearXNG:** the `openphalanx-searxng` container is pinned by digest and runs on the private `openphalanx` Docker network with **no published ports**. Its settings live in `crates/openphalanx-core/searxng/settings.yml` and are written to `~/.local/share/openphalanx/searxng/settings.yml` (0644, read-only mount, `FORCE_OWNERSHIP=false`). The secret is passed as `SEARXNG_SECRET`, and `--log-driver none` keeps queries out of logs. The GUI starts it before the backend when `settings.web_search` is on (the default; toggle on the Server page). The backend gets `SEARXNG_URL=http://openphalanx-searxng:8080`.
* **Gateway:**
  * `POST /v1/search {query, max_results}` returns `{results: [{title, url, snippet}]}`.
  * A chat request with `X-Oppx-Web-Search: auto` (sent by `oppx` unless `--no-web`) goes through the **router**, in two steps:
    1. **Decide:** a `yes`/`no` call to the same model (`gateway.classify`: one regex-constrained token, or a short free answer on reasoning models; see ask/edit routing in the `oppx-chat-frontend` topic). It reads the user's **whole latest turn**, every user message since the last assistant message, minus any paragraph that repeats the system prompt: Aider appends its editing rules to each message, and the router read those instead of the question. The few-shot examples sit in a fixed system prompt, so they're cached. Median about 50 ms (about 260 ms on gpt-oss); 16/16 correct on mixed phrasings for both.
    2. **Query:** only on `yes`, a call writes the search query (constrained JSON, or free text on reasoning models). It's told today's date, and `drop_invented_dates` removes dates and years the model added that the user didn't write. gpt-oss stamped queries with "2024-10-04" or today's date and found stale pages.
    3. **Results** say when they were retrieved, that they beat training data for things that change (versions, releases), and that links can't be opened. gpt-oss otherwise tried to browse and returned nothing.
  * **Speculative routing:** the answer starts at the same time as the router, and its response is held unread until the router decides. With no search, the router costs **about 27 ms** of time-to-first-token (68 ms against 39 ms measured). With a search, the speculative answer is closed (SGLang aborts it) and the request is resent with `<web_search_results>` appended to the end of the latest message, with an "untrusted" note.
  * Native tool calling was tried and doesn't work with Qwen2.5-Coder-14B.
* **Search guard** (`docker/server/searchguard.py`, used by `gateway.web_search`, so by `/v1/search` and the router alike): public engines suspend an address that asks too often, and SearXNG then returns an empty list, which a client can't tell from "nothing found". A day of agent evals did exactly that (Brave and Google CSE "Suspended: too many requests", DuckDuckGo timing out), and one agent turn then reworded an empty search 41 times.
  * **Cache:** the same query within 15 minutes is answered from memory, 256 queries at most; nothing on disk. "The same" ignores case, punctuation, word order and filler words, and from four words up also covers nearly the same words (`NEAR`, 0.8 of all the words either query has): agents reword a search far more often than they ask something new. Shorter queries must match word for word ("vite latest release" is not "react latest release").
  * **Pace:** a burst of 6 searches, then one every 20 s, for the whole server. Over that the search is refused with `503` and `retry-after`, not queued.
  * **Pause:** engines refusing (no results and every engine in SearXNG's `unresponsive_engines`) twice in a row pauses search for 5 minutes, doubling up to an hour while the probe after each pause is refused too. During a pause nothing is sent, the `503` says why ("web search is paused for 4 min: the search engines are refusing requests (brave: Suspended: too many requests; …)"), and the router isn't asked either. An empty answer from engines that did respond is a result, and is cached.
  * **Settings:** `SEARCH_CACHE_S`, `SEARCH_BURST`, `SEARCH_REFILL_S`, `SEARCH_TRIP_AFTER`, `SEARCH_PAUSE_S`, `SEARCH_PAUSE_MAX_S`. `GET /admin/status` has a `search` block (paused, for how long, the reason, sent, from cache, paced); the app doesn't show it yet.
  * **Client:** a search with nearly the words of an earlier one in the session (0.6 of them shared; `Agent._searched`, the latest 30) gets the earlier results back with a note, without a request. The agent stops offering `web_search` until the server's `retry-after` has passed, in this turn and the next ones (`Agent._search_off_until`); three searches in a row with no results, or 8 in one turn (`MAX_SEARCHES`), take the tool away for the turn. `oppx search` prints the server's reason.
  * **Tested:** `scripts/test_searchguard.py` (7 tests, no dependencies, in CI), and a second gateway from the working tree on spare ports in front of a fake SearXNG: a repeat from the cache (1 query reached the engine for 2 requests), the 7th search in a burst refused with `retry-after: 5`, two refusals then a pause (2 queries reached the engine for 4 requests), a cached query still answered while paused, and search back after the pause. Then on the real 3090 in the rebuilt image (the lifecycle example, Qwen2.5-Coder-14B at 32k): after two searches `GET /admin/status` showed `paused: true` with "brave: Suspended: too many requests; duckduckgo: CAPTCHA; google cse: Suspended: too many requests". `searchguard.py` is a new build file, so the image tag changes and every cluster member needs the same code; a split start on the new image hasn't been run yet.
  * **When testing, don't search in a loop.** `scripts/eval_app.py` runs without web search unless `--web` is given.
* **Client:** `oppx`, `oppx aider` and `oppx proxy` send the header by default; `--no-web` opts out (`--web` is accepted as a hidden no-op). `oppx search` calls `/v1/search`.
* **Manual run (without the GUI):**

  ```bash
  docker network create openphalanx
  docker run -d --name openphalanx-searxng --network openphalanx --log-driver none \
    -e FORCE_OWNERSHIP=false -e SEARXNG_SECRET=$(openssl rand -hex 32) \
    -v $PWD/crates/openphalanx-core/searxng/settings.yml:/etc/searxng/settings.yml:ro \
    searxng/searxng@sha256:c642712fcedcdaa78fac44f71eada86aff510745826ba1bd1a368211fea2ce7f
  # then add to the backend's docker run: --network openphalanx -e SEARXNG_URL=http://openphalanx-searxng:8080
  ```
