"""Measure what ``fylgja hook`` costs per event (WP2.1 budget gate, ``PLAN-final-plan.md`` §4.2).

Dev-only and run by hand; the results go into ``docs/spikes/0002-hook-cost.md``::

    uv run --frozen python scripts/bench_hook.py [-n 300] [--rebuild] [--json]

The script builds a throwaway virtualenv under ``target/bench-venv`` with ``uv venv`` and
``uv pip install .``, a regular (non-editable) install like the one ``uv tool`` makes, but never
``uv tool install``. It then runs the venv's ``fylgja hook Stop`` console script N times
(sequentially) with a small payload on stdin, and records each run's wall time and peak RSS
(``wait4`` rusage). For comparison it also runs the bare interpreter (``python -c pass``) and
the guarded shell form an installer registers. Every write goes to ``target/bench-data``; the
real home directory is never touched.

Budget: wall-time p95 ≤ 60 ms and peak RSS ≤ 25 MB.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV = REPO_ROOT / "target" / "bench-venv"
DATA = REPO_ROOT / "target" / "bench-data"

BUDGET_P95_MS = 60.0
BUDGET_RSS_MB = 25.0

PAYLOAD = json.dumps(
    {
        "session_id": "00000000-0000-4000-8000-000000004101",
        "transcript_path": "/Users/user/.claude/projects/x/00000000-0000-4000-8000-0000.jsonl",
        "cwd": "/Users/user/projects/example-1",
        "permission_mode": "default",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Done.",
    },
    separators=(",", ":"),
).encode()

GUARD = 'f="$FYLGJA_BENCH_BIN"; [ -x "$f" ] || exit 0; exec "$f" hook Stop'

_STRIPPED_PREFIXES = ("CLAUDE", "CMUX_", "VIRTUAL_ENV", "COVERAGE_")
_STRIPPED = {"ITERM_SESSION_ID", "TERM_SESSION_ID", "TERM_PROGRAM", "FYLGJA_SUMMARY"}


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def build_venv(rebuild: bool) -> Path:
    python = VENV / "bin" / "python"
    if rebuild and VENV.exists():
        shutil.rmtree(VENV)
    if not python.exists():
        _run(["uv", "venv", "--quiet", "--python", sys.executable, str(VENV)])
    _run(
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(python),
            "--reinstall-package",
            "fylgja",
            str(REPO_ROOT),
        ]
    )
    script = VENV / "bin" / "fylgja"
    if not script.exists():
        raise SystemExit(f"no console script at {script}")
    return script


def bench_env() -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _STRIPPED and not name.startswith(_STRIPPED_PREFIXES)
    }
    for sub in ("config", "data", "state"):
        env[f"FYLGJA_{sub.upper()}_DIR"] = str(DATA / sub)
    env.update(
        {
            "CLAUDE_CODE_SESSION_ID": "00000000-0000-4000-8000-000000004101",
            "CLAUDE_PID": "4101",
            "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/4101.sock",
            "CMUX_WORKSPACE_ID": "00000000-0000-4000-8000-0000000000a1",
            "CMUX_SURFACE_ID": "00000000-0000-4000-8000-0000000000a2",
            "TERM_PROGRAM": "ghostty",
        }
    )
    return env


def _rss_mb(maxrss: int) -> float:
    # ru_maxrss is in bytes on macOS and in KiB on Linux.
    return maxrss / (1024 * 1024) if sys.platform == "darwin" else maxrss / 1024


def run_once(cmd: list[str], env: dict[str, str], stdin: bytes) -> tuple[float, float, float, int]:
    """``(wall_ms, cpu_ms, peak_rss_mb, exit_code)`` of one run."""
    started = time.perf_counter()
    process = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
    )
    assert process.stdin is not None
    try:
        process.stdin.write(stdin)
        process.stdin.close()
    except BrokenPipeError:
        pass
    _, status, usage = os.wait4(process.pid, 0)
    wall_ms = (time.perf_counter() - started) * 1000
    process.returncode = os.waitstatus_to_exitcode(status)
    cpu_ms = (usage.ru_utime + usage.ru_stime) * 1000
    return wall_ms, cpu_ms, _rss_mb(usage.ru_maxrss), process.returncode


def measure(
    name: str, cmd: list[str], env: dict[str, str], runs: int, stdin: bytes
) -> dict[str, Any]:
    for _ in range(10):
        run_once(cmd, env, stdin)
    walls: list[float] = []
    cpus: list[float] = []
    rss: list[float] = []
    codes: set[int] = set()
    for _ in range(runs):
        wall, cpu, peak, code = run_once(cmd, env, stdin)
        walls.append(wall)
        cpus.append(cpu)
        rss.append(peak)
        codes.add(code)
    cuts = statistics.quantiles(walls, n=100, method="inclusive")
    return {
        "name": name,
        "runs": runs,
        "exit_codes": sorted(codes),
        "wall_ms": {
            "min": min(walls),
            "p50": statistics.median(walls),
            "mean": statistics.fmean(walls),
            "p95": cuts[94],
            "p99": cuts[98],
            "max": max(walls),
        },
        "cpu_ms": {"p50": statistics.median(cpus), "mean": statistics.fmean(cpus)},
        "rss_mb": {"p50": statistics.median(rss), "max": max(rss)},
    }


def _count_envelopes() -> int:
    inbox = DATA / "data" / "hook-inbox" / "claude"
    return len([p for p in inbox.glob("*.json")]) if inbox.is_dir() else 0


def machine() -> dict[str, Any]:
    return {
        "date": time.strftime("%Y-%m-%d"),
        "macos": platform.mac_ver()[0] or platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "load_avg": [round(x, 2) for x in os.getloadavg()],
        "python": platform.python_version(),
        "uv": _run(["uv", "--version"]),
    }


def markdown(results: list[dict[str, Any]], info: dict[str, Any], envelopes: int) -> str:
    lines = [
        f"- Date: {info['date']}; macOS {info['macos']} ({info['machine']}, {info['cpus']} CPUs);"
        f" Python {info['python']}; {info['uv']}.",
        f"- Load average at the start (1/5/15 min): {info['load_avg']}.",
        f"- Envelopes written by the measured and warm-up `hook Stop` runs: {envelopes}.",
        "",
        "| Command | Runs | Exit | p50 ms | p95 ms | p99 ms | max ms | CPU ms (mean) | RSS MB |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        w = r["wall_ms"]
        lines.append(
            f"| {r['name']} | {r['runs']} | {', '.join(map(str, r['exit_codes']))} "
            f"| {w['p50']:.1f} | {w['p95']:.1f} | {w['p99']:.1f} | {w['max']:.1f} "
            f"| {r['cpu_ms']['mean']:.1f} | {r['rss_mb']['max']:.1f} |"
        )
    hook = next(r for r in results if r["name"] == "`fylgja hook Stop`")
    ok_wall = hook["wall_ms"]["p95"] <= BUDGET_P95_MS
    ok_rss = hook["rss_mb"]["max"] <= BUDGET_RSS_MB
    lines += [
        "",
        f"Budget: p95 {hook['wall_ms']['p95']:.1f} ms against {BUDGET_P95_MS:.0f} ms "
        f"({'met' if ok_wall else 'MISSED'}); peak RSS {hook['rss_mb']['max']:.1f} MB against "
        f"{BUDGET_RSS_MB:.0f} MB ({'met' if ok_rss else 'MISSED'}).",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--runs", type=int, default=300, help="measured runs (>= 200)")
    parser.add_argument("--rebuild", action="store_true", help="recreate target/bench-venv")
    parser.add_argument("--json", action="store_true", help="print raw results as JSON")
    args = parser.parse_args()
    if args.runs < 200:
        parser.error("use at least 200 runs")

    info = machine()
    script = build_venv(args.rebuild)
    python = str(VENV / "bin" / "python")
    shutil.rmtree(DATA, ignore_errors=True)
    env = bench_env()
    guarded_env = {**env, "FYLGJA_BENCH_BIN": str(script)}
    results = [
        measure("`python -c pass` (same venv)", [python, "-c", "pass"], env, args.runs, b""),
        measure("`fylgja hook Stop`", [str(script), "hook", "Stop"], env, args.runs, PAYLOAD),
        measure(
            "`sh -c` guard, then `fylgja hook Stop`",
            ["/bin/sh", "-c", GUARD],
            guarded_env,
            args.runs,
            PAYLOAD,
        ),
        measure(
            "`fylgja hook StatusLine`",
            [str(script), "hook", "StatusLine"],
            env,
            args.runs,
            b'{"session_id":"bench"}',
        ),
    ]
    envelopes = _count_envelopes()
    if args.json:
        print(json.dumps({"machine": info, "results": results, "envelopes": envelopes}, indent=2))
    else:
        print(markdown(results, info, envelopes))


if __name__ == "__main__":
    main()
