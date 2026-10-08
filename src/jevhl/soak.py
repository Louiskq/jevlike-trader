"""Soak support: sample this process's memory into the events table, and summarise a run.

RSS is read with the standard library only: /proc on Linux, `ps` on macOS, and as a last
resort the peak RSS from getrusage (which can only grow, so it still shows a leak).
"""

from __future__ import annotations

import asyncio
import math
import os
import resource
import subprocess
import sys
from collections.abc import Callable

RUN_START = "run_start"
MIN_DECISIONS_FOR_STUCK = 50  # fewer decisions than this say nothing about the hold rate


def _rss_proc() -> int | None:
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE") // 1024


def _rss_ps() -> int | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return int(out.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def current_rss_kb() -> int:
    for read in (_rss_proc, _rss_ps):
        kb = read()
        if kb:
            return kb
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak // 1024 if sys.platform == "darwin" else peak  # macOS: bytes, Linux: kB


async def rss_sampler(
    store, clock: Callable[[], float], every_s: float = 60.0,
    rss: Callable[[], int] = current_rss_kb,
) -> None:
    """Log an `rss` event (detail = kB) now and then every every_s, until cancelled."""
    while True:
        try:
            store.log_event(clock(), "rss", str(rss()))
        except Exception as e:  # sampling must never stop the bot
            store.log_event(clock(), "rss_error", f"{type(e).__name__}: {e}")
        await asyncio.sleep(every_s)


def mark_run_start(store, clock: Callable[[], float]) -> None:
    """Log where this run begins: detail = id of the newest decision already stored."""
    last = store.recent_decisions(1)
    store.log_event(clock(), RUN_START, str(last[0]["id"] if last else 0))


def soak_report(store) -> dict:
    """RSS and hold rates for the latest run only (from its run_start marker), so a database
    reused by a restart or a second run does not mix runs."""
    events = sorted(store.events(), key=lambda e: e["id"])
    after_decision_id = 0
    for i in range(len(events) - 1, -1, -1):
        if events[i]["kind"] == RUN_START:
            after_decision_id = int(events[i]["detail"])
            events = events[i + 1:]
            break
    samples = []
    for e in events:
        if e["kind"] == "rss":
            try:
                kb = float(e["detail"])
            except ValueError:
                continue
            if math.isfinite(kb) and kb > 0:
                samples.append(int(kb))
    rows = [r for r in store.decisions() if r["id"] > after_decision_id]
    n = len(rows)
    holds = sum(1 for r in rows if r["action"] == "hold")
    late = sum(1 for r in rows if r["reason"] == "late")
    hold_rate = holds / n if n else None
    warnings = []
    if n >= MIN_DECISIONS_FOR_STUCK and hold_rate in (0.0, 1.0):
        warnings.append(f"WARNING: hold rate stuck at {hold_rate:.0%} over {n} decisions")
    return {
        "rss_start_kb": samples[0] if samples else None,
        "rss_end_kb": samples[-1] if samples else None,
        "rss_max_kb": max(samples) if samples else None,
        "rss_samples": len(samples),
        "decisions": n,
        "hold_rate": hold_rate,
        "late_rate": late / n if n else None,
        "warnings": warnings,
    }
