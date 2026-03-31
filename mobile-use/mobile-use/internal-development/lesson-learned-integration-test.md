# Integration Test Plan: Lesson-Learned Memory System

## Prerequisites

- All 3 phases implemented across these files:
  - **New:** `mineru/ui_auto/lessons/__init__.py`, `types.py`, `scorer.py`, `loader.py`, `recorder.py`
  - **Modified:** `graph/state.py`, `context.py`, `graph/graph.py`, `agents/contextor/contextor.py`, `agents/cortex/cortex.md`, `agents/cortex/cortex.py`, `agents/planner/planner.py`, `pyproject.toml`
- Design doc: `mobile-use/internal-development/lesson-learned-memory-design.md`
- Implementation prompt: `mobile-use/internal-development/lesson-learned-implementation-prompt.md`

## Test 1: `post_executor_tools` Node Latency (no device needed)

```bash
uv run python -c "
import time
from mineru.ui_auto.graph.graph import post_executor_tools_node
from langchain_core.messages import ToolMessage

msg = ToolMessage(content='success', tool_call_id='tc1', name='tap', status='success')

class FakeState:
    executor_messages = [msg]

start = time.perf_counter_ns()
for _ in range(1000):
    post_executor_tools_node(FakeState())
elapsed_ns = time.perf_counter_ns() - start

print(f'post_executor_tools_node: {elapsed_ns / 1000 / 1000:.3f}ms for 1000 calls')
print(f'Per call: {elapsed_ns / 1000 / 1000 / 1000:.4f}ms')
"
```

**Expected:** Per-call < 0.1ms.

---

## Test 2: Unprotected Recorder Call Check (no device needed)

```bash
uv run python -c "
import ast

for filepath in [
    'mineru/ui_auto/agents/contextor/contextor.py',
    'mineru/ui_auto/agents/planner/planner.py',
]:
    with open(filepath) as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            call = node.value
            if isinstance(call, ast.Call):
                func = call.func
                name = ''
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name.startswith('record_') or name.startswith('update_lesson') or name.startswith('update_app'):
                    in_try = any(
                        isinstance(p, ast.Try) and any(
                            p.body[0].lineno <= node.lineno <= (h.end_lineno or 0)
                            for h in p.handlers
                        )
                        for p in ast.walk(tree)
                        if isinstance(p, ast.Try)
                    )
                    status = 'OK' if in_try else 'UNPROTECTED!'
                    print(f'{filepath}:{node.lineno} {name}() — {status}')
print('Done.')
"
```

**Expected:** All calls show "OK".

---

## Test 3: Full Lesson Lifecycle Unit Test (no device needed)

```bash
uv run python -c "
import asyncio, tempfile, json
from pathlib import Path
from datetime import datetime, UTC

async def test_full_lifecycle():
    from mineru.ui_auto.lessons.recorder import (
        record_no_effect_mistake, record_mistake_from_tool_failure,
        record_subgoal_failure, record_strategy, update_lesson_feedback,
        capture_screen_signature, update_app_meta, cleanup_stale_lessons,
        _tap_no_effect_counts,
    )
    from mineru.ui_auto.lessons.loader import load_lessons_for_app, load_and_compact_lessons
    from mineru.ui_auto.lessons.types import ScreenSignature

    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        pkg = 'com.test.app'
        sig = ScreenSignature(activity='com.test/.MainActivity', key_elements=['Home', 'Search'])

        # 1. Record app meta
        await update_app_meta(d, pkg, app_version='1.0.0')
        meta = json.loads((d / pkg / '_meta.json').read_text())
        assert meta['app_version'] == '1.0.0'
        print('1. App meta: OK')

        # 2. Record tap-no-effect (needs 2 occurrences)
        _tap_no_effect_counts.clear()
        await record_no_effect_mistake(d, pkg, sig, 'Navigate to settings')
        assert not (d / pkg / 'lessons.jsonl').exists()
        await record_no_effect_mistake(d, pkg, sig, 'Navigate to settings')
        assert (d / pkg / 'lessons.jsonl').exists()
        print('2. Tap no-effect (2nd occurrence): OK')

        # 3. Record tool failure
        await record_mistake_from_tool_failure(d, pkg, 'tap', 'Element not found', sig, 'Tap submit')
        print('3. Tool failure: OK')

        # 4. Record subgoal failure
        await record_subgoal_failure(d, pkg, 'Open Wi-Fi settings', 'Could not find toggle', 'Tried tapping network')
        print('4. Subgoal failure: OK')

        # 5. Record strategy
        await record_strategy(d, pkg, 'Use search instead of scrolling', sig, 'Find contact')
        print('5. Strategy: OK')

        # 6. Load and verify
        lessons = await load_and_compact_lessons(d / pkg / 'lessons.jsonl')
        assert len(lessons) == 4
        assert all(l.app_version == '1.0.0' for l in lessons)
        print(f'6. Load + version tagging ({len(lessons)} lessons): OK')

        # 7. Load formatted for Cortex
        text = await load_lessons_for_app(d, pkg, 'Navigate to settings', None, [])
        assert text is not None
        assert '**Mistakes to avoid:**' in text
        assert '**Proven strategies:**' in text
        print('7. Formatted lesson text: OK')

        # 8. Update feedback
        lesson_id = lessons[0].id
        await update_lesson_feedback(d, pkg, applied_ids=[lesson_id], failed_ids=[], screen_changed=True, tool_status='success')
        updated = await load_and_compact_lessons(d / pkg / 'lessons.jsonl')
        matched = [l for l in updated if l.id == lesson_id]
        assert matched[0].applied_success >= 1
        print('8. Feedback update: OK')

        # 9. Index exists
        index = json.loads((d / '_index.json').read_text())
        assert pkg in index['apps']
        print('9. Index: OK')

        # 10. Cleanup
        removed = await cleanup_stale_lessons(d)
        print(f'10. Cleanup (removed {removed}): OK')

        print('\nAll lifecycle tests passed!')

asyncio.run(test_full_lifecycle())
"
```

**Expected:** All 10 steps print "OK".

---

## Test 4: Zero Behavior Change (requires device)

Run the agent with default config (`lessons_dir=None`). All lesson code is no-op.

```bash
uv run ui-auto --goal "Open the Settings app" --device-type limrun --limrun-platform android
```

**Expected:** Agent completes normally. No lesson-related log lines. No `./lessons/` directory created.

---

## Test 5: Lessons Enabled — Recording (requires device)

The CLI doesn't have a `--lessons-dir` flag yet. Use this test script which patches `MobileUseContext`:

```bash
cat > /tmp/test_lessons.py << 'PYEOF'
import asyncio
import os
from pathlib import Path

async def main():
    import mineru.ui_auto.sdk.agent as agent_mod

    LESSONS_DIR = Path("./lessons")
    LESSONS_DIR.mkdir(exist_ok=True)

    _original_model_post_init = agent_mod.MobileUseContext.model_post_init
    def _patched_post_init(self, *args, **kwargs):
        _original_model_post_init(self, *args, **kwargs)
        object.__setattr__(self, 'lessons_dir', LESSONS_DIR)

    agent_mod.MobileUseContext.model_post_init = _patched_post_init

    from mineru.ui_auto.sdk import Agent
    from mineru.ui_auto.sdk.builders import Builders
    from mineru.ui_auto.sdk.types.task import AgentProfile
    from mineru.ui_auto.config import initialize_llm_config

    llm_config = initialize_llm_config()
    profile = AgentProfile(name="default", llm_config=llm_config)
    config = Builders.AgentConfig.with_default_profile(profile=profile).build()

    agent = Agent(config=config)
    await agent.init()

    task = agent.new_task("Open Settings and tap on Wi-Fi")
    task.with_name("lessons-test").with_trace_recording(path="traces")
    await agent.run_task(request=task.build())
    await agent.clean()

    print("\n=== LESSON FILES ===")
    for p in LESSONS_DIR.rglob("*"):
        if p.is_file():
            print(f"  {p} ({p.stat().st_size} bytes)")
            if p.suffix in ('.jsonl', '.json'):
                print(f"    Content: {p.read_text()[:500]}")

asyncio.run(main())
PYEOF
uv run python /tmp/test_lessons.py
```

**Expected:**
- `./lessons/_index.json` created
- `./lessons/{app_package}/_meta.json` created
- `./lessons/{app_package}/lessons.jsonl` created if any tool failures or tap-no-effect occurred
- No errors in agent logs

---

## Test 6: Lessons Enabled — Loading on Second Run (requires device)

Run the same script again:

```bash
uv run python /tmp/test_lessons.py
```

**Expected:**
- Lessons from Test 5 are loaded into the Cortex prompt
- Look for "Lessons Learned" in the cortex prompt (enable debug logging or check traces)
- Agent should complete normally

---

## What To Do If Tests Fail

- **Import errors:** Run `uv run ruff check mineru/ui_auto/lessons/` to check for syntax issues
- **Lesson not recording:** Check agent logs for `logger.warning("Failed to record...")` — the try/except wrapping will surface the root cause
- **Cortex not showing lessons:** Verify `focused_app_info` is not None in state (lessons load based on current app package)
- **Performance regression:** Profile the Contextor node; screen change detection should add < 5ms per cycle
