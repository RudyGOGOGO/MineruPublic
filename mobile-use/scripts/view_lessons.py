#!/usr/bin/env python3
"""View recorded lessons from the lesson-learned memory system.

Usage:
    python scripts/view_lessons.py ./lessons/com.android.settings/lessons.jsonl
    python scripts/view_lessons.py ./lessons/  # scans all apps
"""
import json
import sys
from pathlib import Path


def view_file(jsonl_path: Path):
    print(f"\n=== {jsonl_path.parent.name} ===\n")
    with open(jsonl_path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            t = e.get("type", "?")
            icon = {
                "mistake": "X",
                "strategy": "!",
                "success_path": "->",
                "ui_mapping": "#",
            }.get(t, "?")
            summary = e.get("summary", "")[:90]
            confidence = e.get("confidence", 0)
            occurrences = e.get("occurrences", 0)
            print(f"  [{icon}] {t:14s} | conf={confidence:.2f} seen={occurrences}x | {summary}")

            if t == "success_path" and e.get("path"):
                for step in e["path"]:
                    action = step.get("action", "?")
                    target = step.get("target_text") or step.get("target_resource_id") or "?"
                    print(f"        step: {action}('{target}')")

    print()


def view_file_raw(jsonl_path: Path):
    print(f"\n=== {jsonl_path.parent.name} ===\n")
    with open(jsonl_path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            print(f"--- Entry {i} ---")
            print(json.dumps(e, indent=2, ensure_ascii=False))
            print()


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/view_lessons.py <lessons.jsonl or lessons_dir> [--raw]")
        sys.exit(1)

    raw = "--raw" in sys.argv
    target = Path(sys.argv[1])

    viewer = view_file_raw if raw else view_file

    if target.is_file() and target.suffix == ".jsonl":
        viewer(target)
    elif target.is_dir():
        found = False
        for jsonl in sorted(target.rglob("lessons.jsonl")):
            viewer(jsonl)
            found = True
        if not found:
            print(f"No lessons.jsonl files found in {target}")
    else:
        print(f"Not a valid path: {target}")
        sys.exit(1)


if __name__ == "__main__":
    main()
