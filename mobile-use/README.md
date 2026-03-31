# Mineru UI-Auto

**Mineru UI-Auto** is a customized fork of [Minitap's mobile-use](https://github.com/minitap-ai/mobile-use), an open-source LangGraph-based agent framework for autonomous Android device control. We've built significant enhancements on top of the original project to improve perception accuracy, model flexibility, and real-world reliability.

## What We Enhanced (vs. Original mobile-use)

### 1. Enhanced Perception: UIAutomator + OCR + SoM (Set-of-Mark)

The original mobile-use relies solely on UIAutomator2 for screen understanding. This works for native Android apps but **fails completely** on WebViews, Canvas/GL views (Google Maps), and dynamically loaded content — UIAutomator returns zero elements for these surfaces.

**Our enhancement** adds an optional dual-perception pipeline:

```
Original:    UIAutomator2 → JSON hierarchy + screenshot → LLM

Enhanced:    UIAutomator2 ─┐
                           ├─ Merge & Dedup → SoM Overlay → LLM
             PaddleOCR ────┘
```

| Capability | Original mobile-use | Mineru UI-Auto (Enhanced) |
|---|---|---|
| Native app elements | UIAutomator only | UIAutomator + OCR verification |
| WebView content (Chrome, Gmail body) | Blind — zero elements | OCR detects all visible text |
| Canvas/GL (Google Maps, games) | Blind — zero elements | OCR reads map labels, POI names |
| Loading screens / animations | Blind — spinner elements only | OCR reads loading text |
| Element targeting | Multi-field fallback (resource_id → bounds → text) | Single `element_id` from numbered SoM overlay |
| Tap accuracy | Stale coordinates from hierarchy dump | Fresh pixel coordinates from current screenshot |

The enhanced mode is **opt-in** (`MOBILE_USE_PERCEPTION=enhanced`) — classic mode remains the default with zero changes to existing behavior.

### 2. Claude CLI Integration (No API Key Required)

The original mobile-use requires an Anthropic API key or OpenAI API key. We integrated **Claude Code CLI** (`claude -p`) as a model provider, enabling all text-heavy agents (Planner, Orchestrator, Executor) to run via a Claude Max subscription — no API key needed.

- Prompts sent via **stdin** (not CLI args) to avoid macOS argument length limits on large prompts
- **Tool schema optimization**: stripped runtime-injected parameters (State, ToolCallId) from tool schemas, reducing executor prompt from **210k tokens to ~8k tokens** (96% reduction)
- Configurable model selection: `--claude-model haiku` / `sonnet` / `opus`

### 3. GUI Owl VLM Integration (Local Visual Grounding)

Added support for a local 8B Vision-Language Model (GUI Owl) running on Apple Silicon via mlx-vlm, providing visual element grounding without cloud API calls:

- **Cortex agent**: Uses VLM to analyze screenshots + UI hierarchy for structured decisions
- **Tap tool**: Automatic `find_element_by_vision` call before every tap for coordinate verification
- Prompt-based JSON extraction with lenient fallback parser for the 8B model's limitations

### 4. Lesson-Learned Memory (Continuous Self-Improvement)

The original mobile-use starts from zero knowledge every run. Our **lesson-learned memory** system records mistakes, strategies, and successful navigation paths across sessions, so the agent gets smoother and faster at UI automation over time.

```
Run 1:  Tap "Account" → wrong screen → back → Tap "Privacy" → wrong screen → back → Tap "Location" → done
Run 2:  (lessons loaded) → Tap "Location" → done   ← follows proven path, skips wrong turns
```

**How it works:**

| Phase | What it records | When |
|-------|----------------|------|
| Mistake detection | Taps with no effect, tool failures, failed subgoals | Automatically, every cycle |
| Strategy detection | Breakthrough actions that unstick the agent | When screen changes after 2+ stuck cycles |
| Success path recording | Clean navigation routes (wrong turns eliminated) | On subgoal completion |
| Confidence feedback | Cortex reports which lessons helped or failed | Next cycle after applying a lesson |

**Key properties:**
- **Per-app JSONL storage** — lessons are scoped to each app package, compacted on read
- **Wrong-turn elimination** — stack-based algorithm removes dead-end navigation (tap → back pairs) from recorded paths
- **Staleness eviction** — UI mappings expire after 30 days, success paths after 60, strategies after 90
- **Token-budgeted injection** — lessons are scored, ranked, and fit within a 500-token budget in the Cortex prompt with type-diversity guarantees (reserved slots prevent any single type from crowding out others)
- **Zero LLM cost** — all recording uses structured tool call data, no LLM summarization
- **Opt-in** — gated behind `lessons_dir` configuration; disabled by default with zero overhead

### 5. App Self-Learning (Automated Exploration)

Beyond learning from human-directed tasks, the agent can **autonomously explore** an app's UI to build comprehensive navigation knowledge *before* real tasks arrive. This transforms real tasks from blind exploration into fast lookups.

```
Without self-learning:
  Task: "Update mailing address"   → 22 steps, 6 wrong turns, times out

After a 30-minute exploration session:
  Task: "Update mailing address"   → 9 steps, 0 wrong turns, succeeds
```

**How it works:**

The self-learning system runs a goal-generation loop around the existing agent:

1. **Screen Discovery** — Crawls the UI hierarchy to find navigable elements (buttons, menu items), using structural deduplication and list-view sampling to prevent explosion on dynamic content (e.g., email inboxes)
2. **Smart Goal Generation** — Uses an LLM to generate context-aware exploration goals considering already-explored siblings and prior lessons (falls back to templates if LLM is unavailable)
3. **Agent Execution** — Dispatches each goal through the existing agent graph (Planner → Cortex → Executor), which records lessons (success paths, mistakes, strategies) as it navigates
4. **Feature Tree Tracking** — Builds a persistent hierarchical map of the app's screens, modals, and features with status tracking (pending/explored/failed/skipped)
5. **Multi-Session Resumability** — Exploration state is saved after every goal; sessions can be interrupted and resumed. BFS maps the top level first, DFS fills in depth on subsequent sessions

**Safety guards** ensure exploration is non-destructive:
- Dangerous actions (delete, reset, send, pay) are blocked at the tool level — the LLM cannot override this
- Discovery-time filtering skips nodes with destructive labels
- Each goal is capped at 20 agent steps
- The app is locked to prevent accidental navigation away

**Dual-App Cluster Mode** supports Parent/Child app ecosystems (e.g., family safety apps):
- A Master Orchestrator coordinates two agents on separate (or shared) devices
- After trigger-worthy actions (enable, share, block, etc.), Observer Mode polls the other app for cross-app effects
- Identity-based diffing prevents false positives from dynamic elements (map avatars, live counters)
- Cross-app triggers are recorded and surfaced as lessons during real tasks

**Exploration Quality Metrics** provide automated assessment:
- Coverage score (explored vs. reachable nodes)
- Path redundancy detection (duplicate success paths)
- Staleness tracking (days since last session + app version drift)
- Re-exploration suggestions for branches with high failure rates

#### Triggering Self-Learning

**Single-app exploration:**

```bash
# Explore Settings app for 30 minutes (one session)
ui-auto learn com.android.settings \
  --lessons-dir ./lessons \
  --model-provider claude --claude-model claude-sonnet-4-6

# Run 3 sessions of 30 minutes each
ui-auto learn com.android.settings \
  --lessons-dir ./lessons \
  --sessions 3

# Custom budget and depth
ui-auto learn com.android.settings \
  --lessons-dir ./lessons \
  --budget-minutes 60 \
  --max-depth 5

# Force depth-first strategy (default: auto — BFS first session, DFS later)
ui-auto learn com.android.settings \
  --lessons-dir ./lessons \
  --strategy depth_first

# Discard prior exploration state and start fresh
ui-auto learn com.android.settings \
  --lessons-dir ./lessons \
  --reset
```

**Dual-app cluster exploration:**

```bash
# Explore two linked apps with cross-app event detection
ui-auto learn-cluster \
  --primary com.example.parent \
  --secondary com.example.child \
  --lessons-dir ./lessons \
  --budget-minutes 45

# With separate devices
ui-auto learn-cluster \
  --primary com.example.parent \
  --secondary com.example.child \
  --primary-device SERIAL1 \
  --secondary-device SERIAL2 \
  --lessons-dir ./lessons

# Custom trigger hints (actions that may cause cross-app effects)
ui-auto learn-cluster \
  --primary com.example.parent \
  --secondary com.example.child \
  --lessons-dir ./lessons \
  --trigger-hints "enable,disable,send,lock"
```

**Check exploration progress:**

```bash
# Visual tree view
python scripts/view_exploration.py ./lessons/com.android.settings/

# Quality report with coverage, redundancy, staleness, and recommendations
python scripts/exploration_report.py ./lessons/com.android.settings/
```

Example report output:
```
============================================================
  Exploration Quality Report: com.android.settings
============================================================

  Coverage:    82.1% (32/39 reachable nodes) [FAIR]
  Redundancy:  15.0% of paths are duplicates [OK]
  Staleness:   3 days since last session [OK]
  App Version: 14.2.1 (at exploration time)

  Recommendations:
    - Run more exploration sessions to increase coverage
    - Re-explore failed branches after app update (may now be accessible)
```

**Session handoff and resumption:**

Exploration is designed for multi-session use — each session picks up exactly where the last one left off. Three per-app files in `lessons/<app_package>/` make this work:

| File | Purpose |
|------|---------|
| `_exploration.json` | Full feature tree with node statuses (machine-readable, loaded automatically on resume) |
| `_handoff.md` | Human-readable session summary: progress, stop reason, next nodes, resume command |
| `lessons.jsonl` | Recorded lessons (mistakes, success paths, strategies — persists across sessions) |

After each session (or when stopped by rate limit / Ctrl+C), a `_handoff.md` file is written:

```markdown
# Exploration Handoff: com.google.android.deskclock

**Last session:** 2026-03-27T05:45:00+00:00
**Sessions completed:** 1
**Stop reason:** unrecoverable_error: rate limit reached

## Progress
- Coverage: 19% (8/42 reachable nodes)
- Explored: 8, Pending: 31, Failed: 2

## Next Nodes to Explore
- com.google.android.deskclock home > **Stopwatch**
- com.google.android.deskclock home > **Bedtime**
- ...

## Resume Command
ui-auto learn com.google.android.deskclock \
  --lessons-dir ./lessons \
  --model-provider claude --claude-model claude-sonnet-4-6
```

When you run the resume command, `load_exploration_state()` loads `_exploration.json`, resets any `in_progress` nodes back to `pending` (crash recovery), and `pick_next_node()` continues from the first unexplored node. No manual intervention needed — just re-run the same command.

The exploration loop also detects **unrecoverable errors** (rate limits, quota exceeded, auth failures) and stops the session immediately instead of retrying. The current node is left as `pending` for the next session.

**CLI reference — `learn` command:**

```
ui-auto learn [OPTIONS] APP_PACKAGE

Arguments:
  APP_PACKAGE                   Android package to explore (e.g., com.android.settings)

Options:
  --lessons-dir TEXT            Directory for lessons and exploration state [required]
  --sessions INT                Number of sessions to run [default: 1]
  --budget-minutes INT          Time budget per session in minutes [default: 30]
  --max-depth INT               Maximum depth in the feature tree [default: 4]
  --strategy TEXT               Node selection: 'auto', 'breadth_first', or 'depth_first' [default: auto]
  --reset                       Discard existing exploration state and start fresh
  -m, --model-provider TEXT     Model provider preset [default: default]
  --claude-model TEXT           Claude model ID
```

**CLI reference — `learn-cluster` command:**

```
ui-auto learn-cluster [OPTIONS]

Options:
  --primary TEXT                Primary app package (active explorer) [required]
  --secondary TEXT              Secondary app package (observer + explorer) [required]
  --primary-device TEXT         ADB device serial for primary app
  --secondary-device TEXT       ADB device serial for secondary app
  --lessons-dir TEXT            Directory for lessons and exploration state [required]
  --sessions INT                Number of sessions to run [default: 1]
  --budget-minutes INT          Time budget per session in minutes [default: 45]
  --max-depth INT               Maximum depth in the feature tree [default: 4]
  --observer-timeout FLOAT      Seconds to wait for cross-app events [default: 10.0]
  --trigger-hints TEXT          Comma-separated labels that trigger Observer Mode
  --reset                       Discard existing exploration state for both apps
  -m, --model-provider TEXT     Model provider preset [default: default]
  --claude-model TEXT           Claude model ID
```

### 6. 5-Stage Tap Fallback Chain

The original mobile-use has a 3-stage tap fallback. We extended it to 5 stages:

```
Stage -1: element_id (enhanced perception) → direct pixel coordinate from SoM
Stage  0: GUI Owl VLM (visual grounding)   → vision-verified coordinates
Stage  1: bounds (coordinates)              → center of bounding box
Stage  2: resource_id                       → accessibility selector
Stage  3: text                              → text content selector
```

Each stage falls through to the next on failure, maximizing tap success rate.

---

## Architecture Overview

```
+------------------+     +-------------------+     +------------------+
|   ui-auto     |     | GUI Owl VLM       |     | Claude CLI       |
|   (agent graph)  |     | 127.0.0.1:8080    |     | claude -p stdin  |
+------------------+     +-------------------+     +------------------+
        |                        |                        |
        |  Cortex (VLM primary)  |  Planner (text)        |
        |  Tap tool (grounding)  |  Orchestrator (text)   |
        |                        |  Executor (tool calls) |
        |                        |  Hopper / Outputter    |
        +------------------------+------------------------+
                         |
                   Android Device
                   (USB / ADB)
```

### How each model is used

- **GUI Owl VLM** (visual grounding model, 8B params):
  - **Cortex agent**: Analyzes screenshot + UI hierarchy to make UI decisions (with structured output via prompt-based JSON extraction + lenient fallback parser)
  - **Tap tool**: Automatically called before every tap for visual coordinate verification (`find_element_by_vision`) — takes a screenshot, sends to VLM, gets `[x, y]` on a 1000x1000 grid, maps to device pixels
- **Claude CLI** (text reasoning via `claude -p` with stdin):
  - **Planner**: Decomposes task into subgoals
  - **Orchestrator**: Manages subgoal execution flow
  - **Executor**: Translates cortex decisions into tool calls (16 tools with large schemas — too big for 8B VLM)
  - **Hopper / Outputter**: Long-context extraction, output formatting

### Why this split?

The 8B GUI Owl VLM has hard constraints:
- **Requires an image** in every request (crashes with `visual_pos_masks` error on text-only)
- **Max ~30k tokens** per request (executor tool schemas alone are 158k tokens)
- **Cannot produce reliable structured JSON** for complex multi-field schemas (uses lenient fallback parser)
- **Strengths**: Visual grounding — given a screenshot + element description, returns precise `[x, y]` coordinates

### Note on Contextor

The contextor agent is configured as `gui_owl` but **does NOT call any LLM** during normal operation. It only gathers device data (UI hierarchy + screenshot). The LLM is only invoked during app lock violations.

### Enhanced Perception Mode (Optional)

An optional **enhanced perception mode** adds PaddleOCR + SoM (Set-of-Mark) overlay on top of the existing UIAutomator pipeline. This fills detection gaps in WebViews, Canvas/GL views, maps, and loading screens.

```
Classic (default):   UIAutomator2 → JSON hierarchy + screenshot → Cortex

Enhanced (opt-in):   UIAutomator2 ─┐
                                   ├─ Merge & Dedup → SoM Overlay → Cortex
                     PaddleOCR ────┘
```

**Key properties:**
- Classic mode is the default — zero changes to existing behavior
- Enhanced mode adds `element_id` targeting (numbered markers on screenshot) alongside existing selectors
- OCR failure is non-fatal — gracefully falls back to UIAutomator-only results
- PaddleOCR is lazy-loaded — users who never enable enhanced mode never pay the import cost

**How to enable:**

```bash
# Via environment variable
MOBILE_USE_PERCEPTION=enhanced ui-auto "open Google Maps and find Starbucks" --model-provider gui_owl

# Via Python SDK
config = AgentConfigBuilder() \
    .with_enhanced_perception() \
    .build()
```

---

## Prerequisites

### System Requirements

| Requirement | Details |
|---|---|
| macOS | Apple Silicon (M1/M2/M3/M4) |
| Python | 3.12+ (ui-auto), 3.11 (mlx-vlm) |
| uv | Package manager ([install](https://github.com/astral-sh/uv)) |
| ADB | Android Debug Bridge ([install](https://developer.android.com/studio/releases/platform-tools)) |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` (needs active Max subscription) |

### Required Python Packages

**ui-auto dependencies** (installed via `uv sync`):

```
langgraph>=1.0.2
adbutils==2.9.3
langchain-google-genai>=4.0.0
langchain>=1.0.0
langchain-core>=1.0.0
langchain-openai>=1.0.0
jinja2==3.1.6
python-dotenv==1.1.1
pydantic-settings==2.10.1
typer==0.16.0
uiautomator2>=3.5.0
httpx>=0.28.1
Pillow  (for screenshot resizing in find_element_by_vision tool)
```

**Enhanced perception mode** (optional, only needed if using `MOBILE_USE_PERCEPTION=enhanced`):

```
paddleocr>=2.7
paddlepaddle>=2.5
Pillow>=10.0
numpy
```

**GUI Owl model server** (installed separately, uses system Python 3.11):

```bash
pip3.11 install mlx-vlm
```

---

## Setup

### 1. Clone and install ui-auto

```bash
git clone https://github.com/mineru-ai/ui-auto.git
cd ui-auto
uv venv
source .venv/bin/activate
uv sync
```

### 2. Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` - for GUI Owl + Claude mode, **no API keys are required** (both are local):

```bash
# .env - minimal config for gui_owl + claude mode

# GUI Owl server URL (MUST use 127.0.0.1, NOT localhost — avoids IPv6 mismatch)
# GUI_OWL_BASE_URL="http://127.0.0.1:8080/v1"
# GUI_OWL_MODEL="MLX_GUI_Owl_8B_16bits"

# Claude model (default: haiku). Override via --claude-model flag or here.
# CLAUDE_MODEL="claude-haiku-4-5-20251001"

# Optional: telemetry
# MOBILE_USE_TELEMETRY_ENABLED="false"

# Optional: save agent thoughts and LLM outputs for debugging
# EVENTS_OUTPUT_PATH="./events.txt"
# RESULTS_OUTPUT_PATH="./results.txt"
```

### 3. Download / verify GUI Owl model

The model weights should be at:
```
/Users/weizhang/workspace/models/gui_owl/MLX_GUI_Owl_8B_16bits/
```

Directory contents (4 safetensors shards, ~16GB total):
```
model-00001-of-00004.safetensors
model-00002-of-00004.safetensors
model-00003-of-00004.safetensors
model-00004-of-00004.safetensors
config.json
tokenizer.json
tokenizer_config.json
preprocessor_config.json
chat_template.jinja
...
```

### 4. Verify Claude CLI

```bash
echo "hello" | claude -p --output-format text
# Should return a greeting (prompt is passed via stdin)
```

---

## Starting the GUI Owl Model Server

Start the mlx-vlm server in a **separate terminal**:

```bash
mlx_vlm.server --port 8080
```

The server will:
- Listen on `http://0.0.0.0:8080` (IPv4)
- Load models on-demand when the first request arrives
- Serve an OpenAI-compatible `/v1/chat/completions` endpoint
- Requests MUST include an image (VLM crashes on text-only requests)

Verify it's running:

```bash
curl http://127.0.0.1:8080/v1/models
```

### Server Options

```bash
# Reduce memory usage with smaller prefill steps
mlx_vlm.server --port 8080 --prefill-step-size 512

# Quantize KV cache to save memory
mlx_vlm.server --port 8080 --kv-bits 4

# Limit KV cache size (in tokens)
mlx_vlm.server --port 8080 --max-kv-size 4096
```

---

## Running Tests

### Connect Android Device

```bash
# Verify device is connected
adb devices
# Should show your device, e.g.:
# 57190DLCR0034P    device
```

### Command Examples

#### Using GUI Owl VLM (recommended for VF test)

```bash
# Basic task with GUI Owl for vision + Claude Haiku for text (default)
ui-auto "Go to Settings and turn on WiFi" --model-provider gui_owl

# With test name and trace recording
ui-auto "Connect to the WiFi network named 'Xhhala' with password '4043171080'" \
  --model-provider gui_owl \
  --test-name wifi_connect_test \
  --traces-path ./traces

# Use a different Claude model for text agents
ui-auto "Go to Settings and turn on WiFi" \
  --model-provider gui_owl \
  --claude-model claude-sonnet-4-6

# Data scraping with structured output
ui-auto "Open Settings and list all available WiFi networks" \
  --model-provider gui_owl \
  --output-description "A JSON list of objects with 'ssid' and 'signal_strength' keys"

# Save agent thoughts for debugging
EVENTS_OUTPUT_PATH=./events.txt \
RESULTS_OUTPUT_PATH=./results.txt \
ui-auto "Open Chrome and navigate to google.com" \
  --model-provider gui_owl
```

#### Using Claude CLI only

```bash
# Claude Haiku for all agents (default, no local model server needed)
ui-auto "Go to Settings and check battery level" --model-provider claude

# Use Sonnet instead
ui-auto "Go to Settings and check battery level" \
  --model-provider claude --claude-model claude-sonnet-4-6

# Use Opus (most capable)
ui-auto "Go to Settings and check battery level" \
  --model-provider claude --claude-model claude-opus-4-6
```

#### With Enhanced Perception (UIAutomator + OCR + SoM)

Enhanced mode adds PaddleOCR + SoM overlay to fill gaps where UIAutomator is blind:
canvas-rendered content, WebViews, maps, and dynamically loaded screens.

```bash
# Native app (baseline — both modes should succeed)
MOBILE_USE_PERCEPTION=enhanced \
ui-auto "Open Settings and find the device name" \
  --model-provider claude --claude-model sonnet \
  --test-name enhanced_settings --traces-path ./traces
```

**Google Maps (Canvas/GL — strongest test case):**
UIAutomator returns zero elements for the map canvas. OCR detects map labels,
POI names, and search results that are completely invisible in classic mode.

```bash
MOBILE_USE_PERCEPTION=enhanced \
ui-auto "Open Google Maps, search for 'Starbucks near me', tell me the name and address of the closest one" \
  --model-provider claude --claude-model sonnet \
  --test-name enhanced_maps --traces-path ./traces
```

**Chrome browser (WebView):**
Web page content is rendered inside a WebView — UIAutomator sees only the
Chrome shell (address bar, tabs). OCR reads the actual page text.

```bash
MOBILE_USE_PERCEPTION=enhanced \
ui-auto "Open Chrome, go to weather.com, and tell me today's temperature" \
  --model-provider claude --claude-model sonnet \
  --test-name enhanced_chrome --traces-path ./traces
```

**Gmail (hybrid — native shell + WebView email body):**
The inbox list is native (UIAutomator works), but email body content is a
WebView. Enhanced mode reads both.

```bash
MOBILE_USE_PERCEPTION=enhanced \
ui-auto "Open Gmail, open the most recent email, and tell me the sender and subject" \
  --model-provider claude --claude-model sonnet \
  --test-name enhanced_gmail --traces-path ./traces
```

**YouTube (Canvas player + dynamic content):**
Video thumbnails, view counts, and titles are often canvas-rendered or
dynamically loaded. OCR catches text that UIAutomator misses during loading.

```bash
MOBILE_USE_PERCEPTION=enhanced \
ui-auto "Open YouTube, search for 'Claude AI', tell me the title and view count of the first result" \
  --model-provider claude --claude-model sonnet \
  --test-name enhanced_youtube --traces-path ./traces
```

**Play Store (dynamic loading + WebView descriptions):**
App descriptions and review text are WebView content. Loading screens show
spinners that UIAutomator can't read, but OCR detects loading text.

```bash
MOBILE_USE_PERCEPTION=enhanced \
ui-auto "Open Play Store, search for 'Spotify', tell me its rating and download count" \
  --model-provider claude --claude-model sonnet \
  --test-name enhanced_playstore --traces-path ./traces
```

**Side-by-side comparison (same task, both modes, with traces):**

```bash
# Classic mode
ui-auto "Open Google Maps, search for 'Starbucks near me', tell me the closest one" \
  --model-provider claude --claude-model sonnet \
  --test-name classic_maps --traces-path ./traces

# Enhanced mode
MOBILE_USE_PERCEPTION=enhanced \
ui-auto "Open Google Maps, search for 'Starbucks near me', tell me the closest one" \
  --model-provider claude --claude-model sonnet \
  --test-name enhanced_maps --traces-path ./traces

# Compare: total steps, tap success rate, task completion, and time
```

#### With Lesson-Learned Memory (Cross-Session Learning)

Lesson-learned memory records mistakes, strategies, and successful navigation paths so the agent
improves across repeated runs on the same app. Enable it with `--lessons-dir` or the
`MOBILE_USE_LESSONS_DIR` environment variable.

**Step 1: First run — the agent explores and learns**

```bash
mkdir -p ./lessons

ui-auto "Open Settings, go to Display settings, and check the current brightness level" \
  --model-provider claude --claude-model claude-sonnet-4-6 \
  --lessons-dir ./lessons \
  --test-name lesson_run_1 --traces-path ./traces
```

**Step 2: Inspect what was recorded**

```bash
# Summary view — shows type, confidence, and success path steps
python scripts/view_lessons.py ./lessons/

# Full raw JSON — see every field of every entry
python scripts/view_lessons.py ./lessons/ --raw

# Specific app only
python scripts/view_lessons.py ./lessons/com.android.settings/lessons.jsonl
```

You should see output like:
```
=== com.android.settings ===

  [X]  mistake        | conf=0.50 seen=1x | Tap had no visible effect on screen (SubSettings)
  [->] success_path   | conf=0.50 seen=1x | Go to Display settings: tap('Display') → tap('Brightness level')
        step: tap('Display')
        step: tap('Brightness level')
```

**Step 3: Second run — the agent follows proven paths**

```bash
ui-auto "Open Settings, go to Display settings, and check the current brightness level" \
  --model-provider claude --claude-model claude-sonnet-4-6 \
  --lessons-dir ./lessons \
  --test-name lesson_run_2 --traces-path ./traces
```

**What to compare between Run 1 and Run 2:**

| Signal | Run 1 (no lessons) | Run 2 (lessons loaded) |
|--------|-------------------|----------------------|
| Total steps | More (trial and error) | Fewer (follows known path) |
| Wrong turns (tap → back) | Likely several | Fewer or zero |
| Cortex prompt | No "Lessons Learned" section | Shows "Known navigation paths", "Mistakes to avoid" |
| Task completion time | Longer | Shorter |

**Step 4: Run on a different app — lessons are per-app**

```bash
# This creates a separate lessons file under ./lessons/com.google.android.gm/
ui-auto "Open Gmail and read the most recent email subject" \
  --model-provider claude --claude-model claude-sonnet-4-6 \
  --lessons-dir ./lessons \
  --test-name lesson_gmail --traces-path ./traces

# Each app accumulates its own lessons independently
ls ./lessons/
```

**Using the environment variable instead of CLI flag:**

```bash
export MOBILE_USE_LESSONS_DIR=./lessons

# All subsequent runs use lessons without --lessons-dir flag
ui-auto "Open Settings and check WiFi status" --model-provider claude
ui-auto "Open Settings and check battery level" --model-provider claude

# Lessons accumulate across runs
wc -l ./lessons/com.android.settings/lessons.jsonl
```

#### What to look for in the logs

| Signal | Classic mode | Enhanced mode |
|---|---|---|
| Element count per screen | UIAutomator only (often 0 on maps/WebView) | UIAutomator + OCR (fills gaps) |
| Tap method | `resource_id=`, `text=`, `coordinates` (multi-stage fallback) | `element_id=N (x, y)` (direct hit) |
| WebView content | Blind — can't read page text | OCR detects all visible text |
| Map labels/POIs | Invisible — canvas rendering | OCR reads map text |
| Failure mode | "No element found" on WebView/canvas | Falls back to classic selectors if element_id fails |

#### Using default config (OpenAI / config file)

```bash
# Uses llm-config.defaults.jsonc or llm-config.override.jsonc
ui-auto "Open the calculator app and compute 42 * 17"
```

### CLI Reference

```
ui-auto [OPTIONS] GOAL

Arguments:
  GOAL                          The main goal for the agent to achieve. [required]

Options:
  -m, --model-provider          Model provider preset:
                                  'default'  - from config files (needs API keys)
                                  'claude'   - Claude CLI for all agents
                                  'gui_owl'  - GUI Owl VLM for vision + Claude for text
                                [default: default]

  --claude-model TEXT           Claude model ID. Overrides CLAUDE_MODEL env var.
                                  Examples: claude-haiku-4-5-20251001 (default),
                                  claude-sonnet-4-6, claude-opus-4-6

  -n, --test-name TEXT          Name for the test recording (enables trace saving)
  -p, --traces-path TEXT        Path to save traces [default: traces]
  -o, --output-description TEXT Natural language description of expected output format
  --lessons-dir TEXT            Directory for lesson-learned memory. Records mistakes,
                                  strategies, and success paths across sessions.
                                  Overrides MOBILE_USE_LESSONS_DIR env var.
  -d, --device-type             'local' or 'limrun' [default: local]
  --with-video-recording-tools  Enable video recording analysis tools
```

---

## Agent-to-Model Mapping

### `--model-provider gui_owl`

| Agent | Provider | Model | Role | Notes |
|---|---|---|---|---|
| Cortex | **gui_owl** | MLX_GUI_Owl_8B_16bits | Analyzes screenshot + UI hierarchy, produces structured decisions | Uses prompt-based JSON extraction with lenient fallback |
| Contextor | gui_owl (configured) | MLX_GUI_Owl_8B_16bits | Gathers device screen data | **Does NOT call LLM** in normal operation |
| Tap tool | **gui_owl** | MLX_GUI_Owl_8B_16bits | Visual element grounding before every tap | Automatic when `gui_owl_enabled=True` |
| Planner | claude | Haiku (default) | Decomposes task into subgoals | Text-only, override with `--claude-model` |
| Orchestrator | claude | Haiku (default) | Manages subgoal execution flow | Text-only, override with `--claude-model` |
| Executor | claude | Haiku (default) | Translates decisions into tool calls | 16 tool schemas = 158k tokens, too large for 8B VLM |
| Hopper | claude | Haiku (default) | Long-context data extraction | Text-only |
| Outputter | claude | Haiku (default) | Formats structured output | Text-only |

### `--model-provider claude`

All agents use Claude CLI (`claude -p` via stdin). Default model: Haiku. Override with `--claude-model`.

---

## Key Implementation Details

### GUI Owl Integration Points

1. **`ChatGuiOwl`** wrapper (`services/gui_owl.py`):
   - Wraps `ChatOpenAI` with `streaming=False` (mlx-vlm streaming is unreliable)
   - `with_structured_output()` → prompt-based JSON extraction, not OpenAI function calling
   - Lenient fallback parser: if JSON extraction fails, constructs a valid schema object from free text
   - `bind_tools()` → injects tool definitions into system prompt
   - Timeout: 300s (prefill can take 2+ min on large screenshots)
   - Base URL: `http://127.0.0.1:8080/v1` (IPv4 explicit — `localhost` resolves to IPv6 first, which mlx-vlm doesn't support)

2. **`find_element_by_vision`** tool (`tools/mobile/find_element_by_vision.py`):
   - Takes a fresh screenshot, resizes to max 980px (avoids MLX cropping bug)
   - Sends to GUI Owl with proven grounding prompt: "Find and point to: {description}"
   - `temperature=0.0`, `max_tokens=50` for deterministic `[x, y]` output
   - Parses coordinates with strict regex + range validation (0-999)
   - Maps 1000x1000 grid to device pixels

3. **Tap tool** fallback chain (`tools/mobile/tap.py`):
   - **Stage -1** (enhanced mode only): Tap by `element_id` from unified element list — looks up element center coordinates
   - **Stage 0** (GUI Owl): When `ctx.gui_owl_enabled=True`, calls `_gui_owl_locate()` for visual grounding
   - **Stages 1-3**: bounds → resource_id → text (existing fallback chain)
   - Each stage falls through to the next on failure

### Claude CLI fixes

- Prompts passed via **stdin** (`subprocess.run(..., input=prompt)`) instead of command-line args — avoids macOS argument length limit on large prompts with base64 screenshots

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `GUI_OWL_BASE_URL` | `http://127.0.0.1:8080/v1` | GUI Owl server base URL (use 127.0.0.1, not localhost) |
| `GUI_OWL_MODEL` | `MLX_GUI_Owl_8B_16bits` | Model name/path sent to mlx-vlm server |
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Claude model ID (overridden by `--claude-model` flag) |
| `EVENTS_OUTPUT_PATH` | (none) | Path to save agent thought traces |
| `RESULTS_OUTPUT_PATH` | (none) | Path to save raw LLM outputs |
| `MOBILE_USE_PERCEPTION` | `classic` | Perception mode: `classic` (UIAutomator only) or `enhanced` (UIAutomator + OCR + SoM) |
| `MOBILE_USE_LESSONS_DIR` | (none) | Directory for lesson-learned memory (overridden by `--lessons-dir` flag) |
| `MOBILE_USE_TELEMETRY_ENABLED` | (prompted) | `true`/`false` for anonymous telemetry |
| `ADB_HOST` | `localhost` | ADB server host |
| `ADB_PORT` | `5037` | ADB server port |

---

## Troubleshooting

### GUI Owl server not responding / Connection error

```bash
# Check if server is running (use 127.0.0.1, NOT localhost)
curl -s http://127.0.0.1:8080/v1/models

# If empty/error, restart:
mlx_vlm.server --port 8080
```

**IPv6 issue**: Python's `httpx` resolves `localhost` to IPv6 `::1` first, but mlx-vlm only listens on IPv4 `0.0.0.0`. The code uses `127.0.0.1` to force IPv4. If you override `GUI_OWL_BASE_URL`, use `127.0.0.1`.

### Streaming error: "An error occurred during streaming"

The `ChatGuiOwl` wrapper uses `streaming=False`. If you still see this, ensure you're using the latest code (the fix is in `services/gui_owl.py` — both `_get_client()` methods set `streaming=False`).

### 500 error: "visual_pos_masks NoneType" / text-only crash

GUI Owl requires an image in every request. The model crashes on text-only prompts. This is why the executor (text-only, 16 tool schemas) must use Claude, not GUI Owl.

### 500 error: 158k+ token prompt

The executor tool schemas total ~158k tokens — far beyond the 8B model's capacity. Only cortex (with screenshot, ~25k tokens) and the tap tool's vision grounding (short prompt + image) should go through GUI Owl.

### Claude CLI fails with exit 1

```bash
# Test CLI directly (prompt via stdin)
echo "hello" | claude -p --output-format text

# Check your Claude Max subscription is active
claude --version
```

### Out of memory on model load

```bash
# Use smaller prefill steps
mlx_vlm.server --port 8080 --prefill-step-size 256

# Or use the 8-bit quantized 32B model instead
# (update GUI_OWL_MODEL in .env)
```

### Device not found

```bash
# Check ADB connection
adb devices

# Restart ADB server
adb kill-server && adb start-server

# For wireless debugging
adb connect <device-ip>:5555
```

---

## Quick Start (Copy-Paste)

```bash
# Terminal 1: Start GUI Owl model server
mlx_vlm.server --port 8080

# Terminal 2: Run automation
cd ui-auto
source .venv/bin/activate
ui-auto "Go to Settings and turn on WiFi" --model-provider gui_owl

# Or with Claude only (Sonnet)
ui-auto "Go to Settings and turn on WiFi" --model-provider claude --claude-model claude-sonnet-4-6
```

## Files Modified/Created (vs. Original mobile-use)

| File | Change |
|---|---|
| `mineru/ui_auto/services/gui_owl.py` | **New** — ChatGuiOwl wrapper (structured output, tool binding, lenient parser) |
| `mineru/ui_auto/services/claude_cli.py` | Fixed: prompt via stdin; `--model` flag passthrough; **tool schema optimization** (210k→8k tokens, strips InjectedState/ToolCallId from schemas) |
| `mineru/ui_auto/services/llm.py` | Added gui_owl provider routing |
| `mineru/ui_auto/tools/mobile/find_element_by_vision.py` | **New** — VLM visual grounding tool |
| `mineru/ui_auto/tools/mobile/tap.py` | Added GUI Owl visual grounding step before tap |
| `mineru/ui_auto/tools/index.py` | Registered find_element_by_vision tool |
| `mineru/ui_auto/config.py` | Added gui_owl provider, `CLAUDE_MODEL` setting, config presets |
| `mineru/ui_auto/context.py` | Added `gui_owl_enabled` flag, `perception_mode` field |
| `mineru/ui_auto/main.py` | Added `--model-provider`, `--claude-model` CLI flags, `ModelProvider` enum |
| `mineru/ui_auto/agents/cortex/cortex.py` | Passes `gui_owl_enabled` and `perception_mode` to template; replaces screenshot with SoM in enhanced mode |
| `mineru/ui_auto/agents/cortex/cortex.md` | Added conditional GUI Owl and Enhanced Perception instructions |
| `mineru/ui_auto/agents/contextor/contextor.py` | Runs enhanced perception pipeline when `perception_mode == "enhanced"` |
| `mineru/ui_auto/graph/state.py` | Added 4 optional enhanced perception state fields |
| `mineru/ui_auto/tools/types.py` | Added `element_id` field to Target model |
| `mineru/ui_auto/tools/mobile/tap.py` | Added element_id tap stage (enhanced mode) + GUI Owl stage |
| `mineru/ui_auto/tools/utils.py` | `has_valid_selectors` recognizes `element_id` |
| `mineru/ui_auto/perception/__init__.py` | **New** — Enhanced perception module exports |
| `mineru/ui_auto/perception/models.py` | **New** — UnifiedElement, EnhancedScreenData dataclasses |
| `mineru/ui_auto/perception/ocr_engine.py` | **New** — PaddleOCR lazy singleton wrapper |
| `mineru/ui_auto/perception/element_merge.py` | **New** — UIAutomator + OCR merge with deduplication |
| `mineru/ui_auto/perception/som_overlay.py` | **New** — SoM numbered marker overlay drawing |
| `mineru/ui_auto/perception/pipeline.py` | **New** — enhance_screen_data() 7-step pipeline |
| `mineru/ui_auto/sdk/types/agent.py` | Added `gui_owl_enabled`, `perception_mode` to AgentConfig |
| `mineru/ui_auto/sdk/builders/agent_config_builder.py` | Added `with_gui_owl()`, `with_enhanced_perception()` builder methods |
| `mineru/ui_auto/sdk/agent.py` | Passes `gui_owl_enabled`, `perception_mode` (with env var override) to MobileUseContext |
| `llm-config.defaults.jsonc` | Added gui_owl config section |
| `mineru/ui_auto/lessons/types.py` | **New** — LessonEntry, PathStep, ScreenSignature data models |
| `mineru/ui_auto/lessons/recorder.py` | **New** — Lesson recording: mistakes, strategies, success paths, feedback updates, staleness cleanup |
| `mineru/ui_auto/lessons/loader.py` | **New** — Lesson loading, compaction, scoring, token-budgeted formatting with type-diversity guarantees |
| `mineru/ui_auto/lessons/scorer.py` | **New** — Multi-signal relevance scoring (screen match, subgoal overlap, confidence, recency, staleness) |
| `mineru/ui_auto/graph/state.py` | Added lesson-learned state fields (screen change detection, action trail, feedback IDs) |
| `mineru/ui_auto/graph/graph.py` | Added action trail capture in post_executor_tools, success path recording in convergence_node |
| `mineru/ui_auto/agents/contextor/contextor.py` | Added screen change detection, lesson loading, mistake/strategy recording, feedback processing |
| `mineru/ui_auto/agents/orchestrator/orchestrator.py` | Added success path signaling on subgoal completion |
| `mineru/ui_auto/agents/cortex/cortex.md` | Added Lessons Learned prompt section with feedback instructions |
| `mineru/ui_auto/exploration/__init__.py` | **New** — Module exports for all exploration submodules |
| `mineru/ui_auto/exploration/types.py` | **New** — FeatureNode, ExplorationState, CrossAppTrigger, ElementDiff, ObserverResult data models |
| `mineru/ui_auto/exploration/discovery.py` | **New** — Feature extraction with list-view sampling, structural IDs, safety filters |
| `mineru/ui_auto/exploration/planner.py` | **New** — BFS/DFS node selection, template-based goal generation |
| `mineru/ui_auto/exploration/runner.py` | **New** — Exploration session lifecycle, agent wrapper, home reset with force-stop fallback |
| `mineru/ui_auto/exploration/state.py` | **New** — Load/save exploration state, crash recovery, tree stats |
| `mineru/ui_auto/exploration/safety.py` | **New** — Tool-level action guard for exploration mode |
| `mineru/ui_auto/exploration/screen_classifier.py` | **New** — Modal vs full-screen vs passive event classification |
| `mineru/ui_auto/exploration/goal_generator.py` | **New** — LLM-powered smart goal generation with lesson context |
| `mineru/ui_auto/exploration/metrics.py` | **New** — Coverage, redundancy, staleness scoring, app version tracking |
| `mineru/ui_auto/exploration/identity.py` | **New** — Element identity keys and identity-based UI diffing |
| `mineru/ui_auto/exploration/observer.py` | **New** — Observer Mode polling with settle window for cross-app events |
| `mineru/ui_auto/exploration/passive_classifier.py` | **New** — Passive event classification (banner, screen change, element update) |
| `mineru/ui_auto/exploration/helpers.py` | **New** — Shared utilities (bounds quantization, key text extraction, coverage) |
| `mineru/ui_auto/exploration/orchestrator.py` | **New** — Master Orchestrator for dual-app cluster mode |
| `mineru/ui_auto/exploration/cross_app_lessons.py` | **New** — Cross-app trigger to lesson conversion |
| `scripts/exploration_report.py` | **New** — CLI exploration quality report |
| `mineru/ui_auto/main.py` | Added `learn` and `learn-cluster` CLI commands |
| `mineru/ui_auto/context.py` | Added `exploration_mode` field |
| `mineru/ui_auto/tools/tool_wrapper.py` | Added exploration guard check |
| `mineru/ui_auto/lessons/loader.py` | Added `cross_app_trigger` lesson type support |
