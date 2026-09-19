# CLAUDE.md — Rocket League Analytics

Project context for Claude Code sessions. Everything below was verified by reading the
repository on this branch. Where something could not be determined from the code, it is
marked **UNVERIFIED** — check before relying on it.

---

## 1. Project Overview

A small, local-first Rocket League replay analyzer with an AI coaching layer on top.
There is no database, no frontend framework, no build step, and no package.json. The whole
app is **6 Python files + 1 HTML page + the `rrrocket` binary**.

Current user experience:

1. User opens `http://localhost:8000/` and sees a single-page upload UI ("Replay Lab").
2. User drops a `.replay` file (or clicks **View example**).
3. The server saves the replay, runs the processing pipeline as a subprocess, and returns
   a URL to a generated self-contained HTML file.
4. The frontend embeds that HTML in an `<iframe>`, an interactive Three.js 3D playback of
   the match with a "coach console" overlay.
5. Beside the viewer, the user asks questions in plain English. A LangGraph agent picks
   deterministic analyses, and the answer streams back with clickable moments that seek the
   3D view and mini-charts drawn from the analysis output. See section 7.

What the analysis system currently does (all inside `Scripts/rl_replay_3d.py`,
function `build_coaching_analytics`, lines ~197-290):

- Per-frame team **goal threat**, **momentum**, and **ball control** time series.
- Per-player report: `impact` series, `boostAverage`, `lowBoostPct`, `speedAverage`,
  `ballProximityPct`, `attackBias`, `impactPeak`.
- Heuristic **events**: `THREAT` windows, `TRANSITION` (fast ball) moments, plus `GOAL`
  events taken from replay metadata.
- Two hardcoded **recommendation** rules (low-boost ≥ 35%, ball proximity < 12%).

These are heuristics derived from position/velocity/boost telemetry, not official stats.
There is **no** touch detection, possession model, rotation model, challenge detection, or
shot detection today. Do not claim otherwise.

What the visualization system does: `rl_replay_3d.py` holds a ~1100-line `HTML_TEMPLATE`
string with embedded CSS/JS. Python resamples telemetry onto a fixed FPS grid, serializes a
JSON payload, downloads three.js + OrbitControls from CDNs at generation time, and does
three `str.replace` substitutions (`__THREE_JS__`, `__ORBIT_JS__`, `__DATA_JSON__`) to
produce one standalone ~2000-line HTML file per replay.

---

## 2. Repository Structure

```
server.py                          stdlib-only HTTP server: static serving + /api/upload
frontend/index.html                the only frontend page (upload UI + iframe viewer)
Scripts/
  run_pipeline.py                  orchestrator: runs parser, then visualizer, as subprocesses
  Replay to CSV Parser.py          .replay -> telemetry CSV + metadata JSON  (NOTE: spaces in filename)
  rl_replay_3d.py                  CSV + JSON -> analytics payload -> standalone 3D HTML
  replay_analysis.py               deterministic analyses for the AI layer (no LLM imports)
  coach_agent.py                   LangGraph agent: picks analyses, writes the answer
  rrrocket.exe / rrrocket          third-party replay decoder binaries (Windows / Linux)
Replay Data/
  Raw Replays/<stem>.replay        uploaded + CLI input replays
  Parsed CSVs/<stem>.csv           per-frame telemetry
  Game Metadata/<stem>.json        match metadata (teams, goals, player stats)
  Replay HTMLs/<stem>.html         generated simulations, served at /simulation/<name>.html
examples/example-replay.html       pre-generated demo, served at /example
examples/example-replay_og.html    older generated demo; not referenced by any code
requirements.txt                   numpy==2.3.5, pandas==2.3.3  (file is UTF-16 encoded)
README.md                          user-facing setup/run docs
.gitignore                         contains only `/Replay Data/`
```

Notes that matter:

- `.gitignore` ignores `/Replay Data/`, but one sample replay set (`265a9dbc-…`) is already
  tracked from before the ignore rule. Do not `git add` new replay artifacts.
- `Scripts/Replay to CSV Parser.py` has **spaces in its filename** and is invoked by path,
  not imported. It is not importable as a module without `importlib`. Keep that in mind
  before assuming `from Scripts... import` will work.
- `__pycache__/` dirs exist and are tracked in places. Ignore them.
- There are **no tests**, no CI config, no linter config, no Dockerfile, no Procfile, and no
  `render.yaml` in the repo.

---

## 3. Actual Execution Flow

```
Browser: drop .replay -> POST /api/upload (multipart, field name "replay")
   |
server.py do_POST
   - validates Content-Length (1 byte .. 500 MB) and multipart content type
   - parses body with email.parser.BytesParser (not cgi)
   - rejects non-.replay suffix
   - saves as "Replay Data/Raw Replays/<safe_stem>-<uuid4[:8]>.replay"
   - subprocess: python Scripts/run_pipeline.py -i <replay_path> -o <html_path>
   |
run_pipeline.py
   [1/2] subprocess: python "Scripts/Replay to CSV Parser.py" --input <replay_path>
   [2/2] subprocess: python "Scripts/rl_replay_3d.py" -i <stem>.csv -o <html_path>
   |
Replay to CSV Parser.py
   - runs rrrocket(.exe) -n <replay> , captures stdout, decodes utf-16/utf-8-sig, json.loads
   - extract_game_metadata(json)  -> metadata dict
   - parse_replay_dict(json)      -> tidy per-update DataFrame (returns None if network_frames is null)
   - complete_replay_data(df)     -> drops players with <100 rows, ffills, builds full time x player grid
   - writes Replay Data/Parsed CSVs/<stem>.csv and Game Metadata/<stem>.json
   |
rl_replay_3d.py
   - load_team_setup(json)  sets module globals ORANGE_TEAM / BLUE_TEAM
   - load_data(csv)         -> DataFrame
   - build_payload(df, fps=30, metadata) -> resample to FPS grid, per-entity arrays
       -> build_coaching_analytics(...) -> {"series", "players", "events", "recommendations"}
   - fetch_js(THREE_CDN), fetch_js(ORBIT_CDN)   <-- REQUIRES INTERNET at generation time
   - writes the standalone HTML to -o path
   |
server.py returns 201 {"filename": "<stem>.html", "simulationUrl": "/simulation/<stem>.html"}
   |
frontend/index.html -> <iframe src=simulationUrl>
```

On pipeline failure the server deletes the uploaded `.replay`, and responds `422` with
`{"error", "details"}` where `details` is the last 4000 chars of the subprocess stderr/stdout.

### HTTP surface (complete — this is all of it)

| Method | Path | Behavior |
|---|---|---|
| GET | `/` | serves `frontend/index.html` |
| GET | `/example` | serves `examples/example-replay.html` |
| GET | `/simulation/<name>.html` | serves from `Replay Data/Replay HTMLs` (path-escape guarded) |
| GET | `/api/example` | `{stem, simulationUrl, askable}` for the example replay |
| POST | `/api/upload` | upload + full pipeline, returns 201/400/413/422 |
| POST | `/api/ask` | AI coaching Q&A, streams NDJSON; 400/404/503 before the stream opens |
| any other | | 404 JSON `{"error": "Not found."}` |

There is no route dispatch table — routing is `if` chains in `do_GET`/`do_POST`
(`server.py:38-140`). A new endpoint means adding another branch there.

---

## 4. Development Commands

Verified from the repo:

```powershell
pip install -r requirements.txt          # numpy + pandas only
python .\server.py                       # serves on 0.0.0.0:$PORT, default 8000
python .\Scripts\run_pipeline.py -i "Replay Data\Raw Replays\match.replay" -o "Replay Data\Replay HTMLs\match.html"
python .\Scripts\"Replay to CSV Parser.py" --input <replay>   # parse only
python .\Scripts\rl_replay_3d.py -i <stem>.csv -o out.html --fps 30
```

```powershell
python .\Scripts\replay_analysis.py <stem>    # every deterministic analysis, no API key needed
$env:ANTHROPIC_API_KEY = "sk-ant-..."         # required only for /api/ask
python .\Scripts\coach_agent.py <stem> "question"
```

- **Tests: none exist.** There is no test runner configured. If asked to verify, run the
  pipeline against `Replay Data/Raw Replays/265a9dbc-6e9f-471e-bd3a-23cfc6ee8a10.replay`
  and/or drive `/api/upload` with curl. `replay_analysis.py` is runnable standalone and is
  the cheapest way to check analysis changes.
- **Port:** `server.py` reads `PORT` from the environment only. README's
  `python .\server.py 8000` is inaccurate — a positional arg is ignored.
- **Build step: none.** No bundler, no npm, no compiled assets.
- **Deployment:** README links a Render deployment
  (`https://rocket-league-analytics.onrender.com/`). No deployment config file exists in the
  repo — build/start commands are presumably set in the Render dashboard. **UNVERIFIED**;
  confirm with the user before changing anything that affects startup (e.g. the `PORT` read,
  the Linux `Scripts/rrrocket` path, or adding dependencies).

---

## 5. Data Structures (reuse these; do not invent parallel ones)

**Telemetry CSV** — `Replay Data/Parsed CSVs/<stem>.csv`, one row per (time, player):

```
time,player,actor_id,pos_x,pos_y,pos_z,vel_x,vel_y,vel_z,boost,throttle,steer
```

- `player == "BALL"` is the ball row (`actor_id` 0).
- `boost` is raw 0-255 in the CSV; `resample_entity` converts to 0-100 for the payload.
- Field frame: x ∈ [-4096, 4096], y ∈ [-5120, 5120] (blue goal at y=-5120, orange at
  +5120), z ∈ [0, 2048]. Constants live at `rl_replay_3d.py:50-63`.

**Metadata JSON** — `Replay Data/Game Metadata/<stem>.json`:

```json
{ "match_id", "match_guid", "date", "map_name", "match_type",
  "team_scores": {"BLUE": int, "ORANGE": int},
  "BLUE_TEAM": [names], "ORANGE_TEAM": [names],
  "goals": [{"frame", "player_name", "team"}],
  "player_stats": [{"name","team","score","goals","assists","saves","shots","bBot"}] }
```

Goal `frame` is a replay frame index; `build_coaching_analytics` converts it with
`frame / 30.0` (assumed 30 fps recording).

**Visualization payload** (`build_payload`, embedded as `__DATA_JSON__`):

```json
{ "meta": {"fps","dt","frames","duration"},
  "field": {...arena dimensions...},
  "teams": {"orange": {"players","color","goalY"}, "blue": {...}},
  "analytics": {"series", "players", "events", "recommendations"},
  "entities": [{"name","type":"ball|car","team","color","x":[],"y":[],"z":[],"yaw":[],"boost":[]}] }
```

**Analytics block** (consumed by the in-HTML JS as `DATA.analytics`, `rl_replay_3d.py:878`):

```json
{ "series": {"orangeThreat","blueThreat","orangeMomentum","blueMomentum",
             "orangeControl","blueControl"},          // each a per-frame float array
  "players": {"<name>": {"team","impact":[],"boostAverage","lowBoostPct",
                         "speedAverage","ballProximityPct","attackBias","impactPeak"}},
  "events": [{"frame","time","kind":"THREAT|TRANSITION|GOAL","team","title","detail"}],
  "recommendations": [{"player","priority","title","detail"}] }
```

**Upload response:** `{"filename": "<stem>.html", "stem": "<stem>", "simulationUrl": "/simulation/<stem>.html"}`.

**Replay identifier:** the *file stem* is the only ID. It ties together
`Raw Replays/<stem>.replay`, `Parsed CSVs/<stem>.csv`, `Game Metadata/<stem>.json`, and
`Replay HTMLs/<stem>.html`. For uploads the stem is `<sanitized-original-name>-<uuid4hex8>`.
Any future AI feature should key off this stem — do not introduce a second ID scheme.

### Known quirks (do not "fix" as drive-by work; mention them if they block you)

- `rl_replay_3d.py:1544` does `csv_path = REPLAY_DATA_DIR / Path(args.input).name` — `-i`
  always resolves inside `Replay Data/Parsed CSVs`, ignoring any directory you pass.
- `ORANGE_TEAM` / `BLUE_TEAM` are module-level globals mutated by `load_team_setup`. Anything
  importing `rl_replay_3d` for reuse must call that first or teams will be `None`.
- `fetch_js` hits two CDNs on every generation; offline runs fail the whole pipeline.
- `requirements.txt` is UTF-16 encoded. Rewriting it carelessly may break `pip install`.
- **`build_coaching_analytics`'s inverted attack direction was fixed.** `attack_direction`
  now matches `replay_analysis.py`'s `ATTACK_SIGN` (BLUE attacks +y toward the orange goal,
  ORANGE attacks -y toward the blue goal), and the orange/blue threat series (previously
  built from the wrong side of the field) were swapped to match. `attackBias` and the
  viewer's threat series / THREAT event team labels now agree with `replay_analysis.py`'s
  AI-facing numbers instead of disagreeing with them.
- Goal timing **was** wrong (`frame / 30` against non-uniform frames, drifting up to 39s) and
  is now fixed via `goal_times_from_crossings`, with a fallback to the old estimate when the
  crossings do not line up with the header.

---

## 6. Engineering Rules for This Repo

This is a deliberately tiny codebase. Keep it that way.

**Minimalism.** Prefer the smallest change that satisfies the request. Do not add
abstractions, generic frameworks, helper classes, interfaces, provider abstractions, config
systems, or new files "for organization" without a present need. Do not add dependencies
without a concrete reason — today the entire backend is stdlib + numpy + pandas.

**Preserve what works.** Reuse existing functions (`process_replay_in_memory`,
`build_payload`, `build_coaching_analytics`, `load_data`) and the existing CSV/JSON/payload
shapes. Do not duplicate analysis logic. Do not rewrite the HTML template, the server, or
the parser because a different architecture looks cleaner.

**Understand before editing.** Read the relevant code, trace the data flow, find reusable
utilities, choose the smallest change — then implement. Don't start by creating files.

**No speculative features.** Do not add auth, a database, a vector DB, RAG, agent memory,
background agents, multi-agent systems, multiple LLM providers, observability stacks, or
caching layers unless a request actually requires them.

**Change discipline.** State the intent before a large change, keep the diff scoped, avoid
unrelated edits and mass rewrites, explain architectural decisions, and surface uncertainty
instead of guessing. If the existing architecture conflicts with a requested feature,
investigate the conflict and raise it rather than silently refactoring around it.

**Accuracy.** If you can't determine something from the code, say so. Don't invent files,
endpoints, or commands.

---

## 7. Stage 3 — AI Coaching Q&A (IMPLEMENTED)

A user can ask a natural-language question about a loaded replay. The answer, its
supporting moments, and any charts stream back into the panel beside the 3D viewer.

```
question + stem -> POST /api/ask -> coach_agent.answer_question()
                                      |
                                      +-- plan (claude-opus-5, tools bound)
                                      +-- ToolNode (deterministic Python)   <-- loops
                                      +-- structure (claude-sonnet-5) -> evidence chips
                                      |
                        <- NDJSON: tool / chart / answer / evidence / error
```

**Core principle, unchanged: Python computes every number.** The model chooses which
analyses to run, interprets their compact output, and writes the explanation. It is
instructed never to calculate a statistic or invent a timestamp.

### The two-payload rule

Every function in `replay_analysis.py` returns `(llm_payload, display_payload)`:

- `llm_payload` — flat scalars, ~40 lines of JSON. This is all the model ever sees.
- `display_payload` — chart series, downsampled to <=120 points. Streamed straight to the
  browser, never into model context.

Keep this split when adding an analysis. It is what stops token cost growing with the
richness of the visuals.

### The four analyses (`Scripts/replay_analysis.py`)

| Function | Returns |
|---|---|
| `match_summary` | official score, per-player goals/assists/saves/shots, corrected goal times |
| `boost_report` | average boost, time starved / at zero / at full, depletion episodes |
| `positioning_report` | thirds, behind-ball %, distance to ball, spacing, speed, man-rank proxy |
| `key_moments` | corrected goals plus heuristic pressure and transition windows |

All of them are time-weighted and skip dead time (`MAX_SAMPLE_GAP`), emit
viewer-relative timestamps, and convert boost from the CSV's 0-255 to 0-100. Those
conversions happen once, in `ReplayContext`, deliberately — do not repeat them per analysis.

`replay_analysis.py` imports no LLM libraries and runs standalone
(`python Scripts/replay_analysis.py <stem>`). Keep it that way: it is the only part of the
AI feature testable without an API key, and it is where analysis changes should be verified.

### What is deliberately NOT computed

The telemetry has no ball-touch, demolition or possession data. `complete_replay_data`
forward-fills through actor deletion, so a demoed car is indistinguishable from a parked
one. Shot and save counts come from the replay header, not from a detector. Do not add
`analyze_shots`, `analyze_challenges` or a possession model without first building touch
inference and being explicit about its error rate. The "first/second/third man" figures are
a distance-to-ball rank proxy and the system prompt requires the model to label them as such.

### Agent shape (`Scripts/coach_agent.py`)

- Two-node LangGraph loop (`plan` -> `tools` -> `plan`), recursion cap 14 (~6 tool rounds).
  There is intentionally **no separate sufficiency-check node** — that would spend an extra
  LLM call per iteration asking a question the model already answers by emitting or not
  emitting a tool call.
- `claude-opus-5` plans and writes the answer; `claude-sonnet-5` makes one structured-output
  call selecting which already-computed moments to surface as chips. Candidate timestamps are
  deterministic; the model only picks among them, and picks that do not match a real
  candidate are dropped.
- The replay stem is bound into the tool closures, so it never occupies context and the model
  cannot get it wrong.
- State is just `messages`. No DataFrames, no series, no replay data.
- Do **not** pass `temperature`, `top_p` or `budget_tokens` to `ChatAnthropic` — all are
  rejected with a 400 on Opus 5. `langchain-anthropic` 1.7.2 omits them when unset; verify
  this still holds if you bump the pin.

### Secrets

`ANTHROPIC_API_KEY` from the environment only, checked before the lazy import of
`coach_agent` so a server without it still serves `/`, `/example`, `/simulation/*` and
`/api/upload` normally and returns a clean 503 from `/api/ask`. Never hardcode it, never
send it to the browser, keep all model calls server-side.

---

## 8. Frontend & Backend Boundaries for Future Work

**Frontend.** `frontend/index.html` (upload shell) and the `HTML_TEMPLATE` in
`rl_replay_3d.py` (per-replay viewer) are the only two UIs. Both are hand-written HTML/CSS/JS
with no framework. Add small, focused elements to what exists. Do not introduce a frontend
framework, rebuild the dashboard, replace working components, add animations, or bloat the
coach panel. AI output should be concise and evidence-backed, not verbose.

Be aware the viewer runs inside an `<iframe>` served from `/simulation/...` and is a static
generated file. An interactive Q&A UI would need either a new endpoint called from the
parent page (`frontend/index.html`) or JS added to the template that calls back to the
server — decide deliberately and say which you chose.

**Backend.** `server.py` keeps replay handling, validation, file I/O, endpoints, and (in
future) LLM calls and agent orchestration. Deterministic replay computation stays in Python;
it does not move into prompts. Note the server is single-file `BaseHTTPRequestHandler` with
blocking `subprocess.run` inside the request — long LLM calls in a request handler will hold
a thread. Consider that before designing a new endpoint.

---

## 9. Verification Requirements

Do not claim something works without exercising it. After changes:

1. Run the pipeline end to end on the tracked sample replay.
2. Start `python .\server.py` and load `/` and `/example`.
3. Exercise any endpoint you touched (including `/api/upload` with a real `.replay`).
4. Open a generated HTML and confirm the 3D playback and coach panel still render.
5. Test new AI functionality separately from the existing pipeline.
6. Read the server/subprocess output — the 422 `details` field carries the real error.
7. Fix root causes, not symptoms. Report failures honestly, with the output.

Remember CDN access is required for step 1 to succeed.
