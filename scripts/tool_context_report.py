"""Report (and optionally re-record) the tool-list context cost of each MCP surface.

    uv run python scripts/tool_context_report.py            # table + diff vs baseline
    uv run python scripts/tool_context_report.py --update   # re-record the baseline
    uv run python scripts/tool_context_report.py --markdown # for $GITHUB_STEP_SUMMARY

The baseline (tests/unit/tool_context_baseline.json) is what
tests/unit/test_tool_context_budget.py holds the surfaces to.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from rootly_mcp_server.tool_context import (
    PER_TOOL_MAX_CHARS,
    SURFACES,
    measure_surface,
)

BASELINE = Path(__file__).resolve().parents[1] / "tests" / "unit" / "tool_context_baseline.json"
TOP = 10


async def _measure_all() -> dict[str, list]:
    return {surface: await measure_surface(surface) for surface in SURFACES}


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--update", action="store_true", help="re-record the baseline")
    parser.add_argument("--markdown", action="store_true", help="emit GitHub-flavored markdown")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)

    measured = asyncio.run(_measure_all())
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}

    if args.update:
        BASELINE.write_text(
            json.dumps(
                {
                    surface: {"tools": len(sizes), "chars": sum(s.chars for s in sizes)}
                    for surface, sizes in measured.items()
                },
                indent=2,
            )
            + "\n"
        )
        print(
            f"Recorded {BASELINE.relative_to(Path.cwd()) if BASELINE.is_relative_to(Path.cwd()) else BASELINE}"
        )

    md = args.markdown
    print("## MCP tool-list context cost\n" if md else "")
    print(
        "| surface | tools | chars | ~tokens | vs baseline |"
        if md
        else f"{'surface':<12} {'tools':>5} {'chars':>9} {'~tokens':>8}  vs baseline"
    )
    if md:
        print("|---|---:|---:|---:|---:|")
    for surface, sizes in measured.items():
        chars = sum(s.chars for s in sizes)
        base = baseline.get(surface, {}).get("chars")
        delta = f"{(chars - base) / base:+.1%}" if base else "n/a"
        row = (surface, len(sizes), chars, chars // 4, delta)
        print(
            "| {} | {} | {:,} | {:,} | {} |".format(*row)
            if md
            else "{:<12} {:>5} {:>9,} {:>8,}  {}".format(*row)
        )

    largest = measured["hosted_full"][:TOP]
    print(
        f"\n{'### ' if md else ''}Largest tools (hosted_full; cap {PER_TOOL_MAX_CHARS:,} chars)\n"
    )
    for size in largest:
        flag = "  OVER CAP" if size.chars > PER_TOOL_MAX_CHARS else ""
        print(
            f"{'- ' if md else '  '}`{size.name}` {size.chars:,} chars (~{size.approx_tokens:,} tokens){flag}"
            if md
            else f"  {size.chars:>8,}  ~{size.approx_tokens:>6,} tok  {size.name}{flag}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
