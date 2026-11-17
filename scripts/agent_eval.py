#!/usr/bin/env python
"""Render the offline agent-evaluation ablation table.

Example:
  python scripts/agent_eval.py --records var/agent-eval/records.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.services.agent_eval import aggregate, load_records, render_markdown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True, help="JSONL replay records")
    parser.add_argument("--json", action="store_true", help="print the raw summary JSON")
    args = parser.parse_args()
    try:
        records = load_records(args.records)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    summary = aggregate(records)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
