#!/usr/bin/env python3
"""Join spike (WP0.3): join the live Claude registry with AeroSpace windows.

Stdlib only; never imports `fylgja`; not packaged. Read-only: it reads the registry
(`~/.claude/sessions/<pid>.json`, the A13 glob only), `aerospace list-*`, `ps`, and the
JetBrains `recentProjects.xml` of the newest product directories. It never reads `*.key`
files and never reads another process's environment.

Usage:
    join_table.py                      read live, print the table
    join_table.py --json               read live, print the rows as JSON
    join_table.py --capture-out DIR    also write the raw inputs to DIR
    join_table.py --from-capture DIR   run the same join on a capture, no live read

The join (`join_rows`) is a pure function over the captured inputs (A1):
  1. walk the session pid's parent chain until a pid owns an AeroSpace window (host app);
  2. cmux: use CMUX_* ids that the process args expose (never the environment);
  3. JetBrains: an opened project whose path contains the cwd, longest prefix wins;
  4. iTerm2: best effort (window title equals the session name, else the only window).
Every row carries `via` and `confidence`; a row that cannot be joined says why.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CMUX_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"
# With --json, aerospace accepts only interpolation variables and spaces in --format.
WINDOW_FORMAT = (
    "%{window-id} %{workspace} %{monitor-id} %{app-bundle-id} %{app-name} %{app-pid}"
    " %{window-title}"
)
# The sanctioned way to say which ids may be taken from a process's arguments (A3 names).
CMUX_ID_NAMES = ("CMUX_WORKSPACE_ID", "CMUX_SURFACE_ID", "CMUX_TAB_ID", "CMUX_PANEL_ID")
CMUX_ID_RE = re.compile(r"\b(" + "|".join(CMUX_ID_NAMES) + r")\W{1,4}([A-Za-z0-9-]{8,64})")
REGISTRY_GLOB = re.compile(r"^[0-9]+\.json$")  # A13: never `*.key`
PS_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(.*?)\s*$")
MAX_CHAIN = 64
TITLE_SPLIT = re.compile(r"\s[–—-]\s")
JETBRAINS_PRODUCT = {"intellij": "intellijidea"}  # bundle-id suffix -> product dir stem

COLUMNS = (
    ("name", 24),
    ("kind/entry", 16),
    ("state", 8),
    ("project", 26),
    ("host", 10),
    ("window", 7),
    ("ws", 4),
    ("via/confidence", 40),
)


# --------------------------------------------------------------------------- pure helpers


def parse_ps_table(text: str) -> dict[int, tuple[int, str]]:
    """Parse `ps -A -o pid=,ppid=,comm=` into {pid: (ppid, comm)}."""
    table: dict[int, tuple[int, str]] = {}
    for line in text.splitlines():
        match = PS_LINE.match(line)
        if match:
            table[int(match[1])] = (int(match[2]), match[3])
    return table


def extract_cmux_ids(args: str) -> dict[str, str]:
    """Return the CMUX_* id names (A3 allowlist only) that a process's args expose."""
    found: dict[str, str] = {}
    for match in CMUX_ID_RE.finditer(args):
        found.setdefault(match[1], match[2])
    return found


def parse_windows(text: str) -> list[dict[str, Any]]:
    """Parse the JSON of `aerospace list-windows --json --format WINDOW_FORMAT`."""
    windows: list[dict[str, Any]] = []
    for item in json.loads(text):
        windows.append(
            {
                "window_id": item.get("window-id"),
                "workspace": item.get("workspace"),
                "monitor_id": item.get("monitor-id"),
                "bundle_id": item.get("app-bundle-id") or "",
                "app_name": item.get("app-name") or "",
                "app_pid": item.get("app-pid"),
                "title": item.get("window-title") or "",
            }
        )
    return windows


def host_kind(bundle_id: str) -> str:
    if bundle_id == "com.cmuxterm.app":
        return "cmux"
    if bundle_id.startswith("com.jetbrains."):
        return "jetbrains"
    if bundle_id == "com.googlecode.iterm2":
        return "iterm2"
    return "other"


def product_stem(dirname: str) -> str:
    """`PyCharm2026.1` -> `pycharm`; `IntelliJIdea2026.2` -> `intellijidea`."""
    return re.sub(r"[\d.]+$", "", dirname).lower()


def version_key(dirname: str) -> tuple[int, ...]:
    tail = re.search(r"([\d.]+)$", dirname)
    if not tail:
        return ()
    return tuple(int(part) for part in tail[1].split(".") if part)


def newest_product_dirs(names: Sequence[str]) -> dict[str, str]:
    """Pick the newest directory name (by version number) per JetBrains product stem."""
    best: dict[str, str] = {}
    for name in names:
        stem = product_stem(name)
        if stem not in best or version_key(name) > version_key(best[stem]):
            best[stem] = name
    return best


def jetbrains_stem_for(bundle_id: str) -> str:
    suffix = bundle_id.removeprefix("com.jetbrains.")
    return JETBRAINS_PRODUCT.get(suffix, suffix)


def parse_recent_projects(xml_text: str, home: str) -> list[dict[str, Any]]:
    """Return the opened projects of one `recentProjects.xml` as {path, frame_title}.

    `frame_title` is often empty. `displayName` is deliberately not used: in the observed
    files it lists module names, not the name that appears in the window title.
    """
    root = ET.fromstring(xml_text)  # noqa: S314 - local IDE file, stdlib parser
    projects: list[dict[str, Any]] = []
    for entry in root.iter("entry"):
        key = entry.get("key")
        meta = entry.find(".//RecentProjectMetaInfo")
        if not key or meta is None or meta.get("opened") != "true":
            continue
        path = key.replace("$USER_HOME$", home).rstrip("/")
        projects.append({"path": path, "frame_title": meta.get("frameTitle") or ""})
    return projects


def title_project(title: str) -> str:
    """The part of a JetBrains frame/window title before ` – <file>`."""
    return TITLE_SPLIT.split(title, maxsplit=1)[0].strip()


TITLE_PATH = re.compile(r"^(?P<name>.*?) \[(?P<path>[^\]]+)\]$")


def window_match(title: str, project: Mapping[str, Any], home: str) -> str | None:
    """How does a JetBrains window title name this project: "path", "name" or not at all?

    JetBrains shows `<name> [<path>]` when two open projects share a name; the path then
    decides exactly. Otherwise only the name is shown (the frame title's project part, or the
    directory name), and two different projects can share it.
    """
    shown = title_project(title)
    clash = TITLE_PATH.match(shown)
    if clash:
        same = clash["path"].replace("~", home, 1).rstrip("/") == project["path"]
        return "path" if same else None
    names = {title_project(project["frame_title"]), os.path.basename(project["path"])}
    names.discard("")
    return "name" if shown in names else None


def path_contains(project: str, cwd: str) -> bool:
    return cwd == project or cwd.startswith(project + "/")


def parent_chain(pid: int, ps: Mapping[int, tuple[int, str]], owners: set[int]) -> list[int]:
    """Pids from `pid` upwards, ending at the first pid in `owners` (if any)."""
    chain: list[int] = []
    current = pid
    while current in ps and current > 1 and len(chain) < MAX_CHAIN and current not in chain:
        chain.append(current)
        if current in owners:
            break
        current = ps[current][0]
    return chain


# --------------------------------------------------------------------------- the join


def _unresolved(row: dict[str, Any], reason: str) -> dict[str, Any]:
    row["via"] = "none"
    row["confidence"] = "none"
    row["unresolved"] = reason
    return row


def _set_window(row: dict[str, Any], win: Mapping[str, Any], via: str, confidence: str) -> None:
    row["window_id"] = win["window_id"]
    row["workspace"] = win["workspace"]
    row["via"] = via
    row["confidence"] = confidence


def _set_workspace_only(
    row: dict[str, Any], windows: Sequence[Mapping[str, Any]], via: str, note: str
) -> bool:
    """All candidate windows share one workspace: the workspace is known, the window is not."""
    spaces = {w["workspace"] for w in windows}
    if len(spaces) != 1:
        return False
    row["workspace"] = next(iter(spaces))
    row["via"] = via
    row["confidence"] = "low"
    row["note"] = note
    return True


def _join_cmux(row: dict[str, Any], windows: Sequence[Mapping[str, Any]]) -> None:
    ids = row["cmux_ids"]
    via = "chain+cmux-ids" if ids else "chain+window"
    # The cmux window is known; the cmux workspace/surface only if an id was exposed.
    if len(windows) == 1:
        _set_window(row, windows[0], via, "high" if ids else "window-only")
        if not ids:
            row["note"] = "cmux workspace/surface not resolved (no CMUX_* id in args)"
        return
    if not _set_workspace_only(row, windows, via, f"{len(windows)} cmux windows; window ambiguous"):
        _unresolved(row, f"{len(windows)} cmux windows on different AeroSpace workspaces")


def _join_jetbrains(
    row: dict[str, Any],
    windows: Sequence[Mapping[str, Any]],
    projects: Sequence[Mapping[str, Any]],
    home: str,
) -> None:
    cwd = row["cwd"]
    if not cwd:
        _unresolved(row, "registry row has no cwd")
        return
    matching = [p for p in projects if path_contains(p["path"], cwd)]
    if not matching:
        _unresolved(row, "no opened JetBrains project contains the cwd")
        return
    project = max(matching, key=lambda p: len(p["path"]))  # longest prefix wins
    row["project"] = os.path.basename(project["path"])
    matches = [(w, window_match(w["title"], project, home)) for w in windows]
    by_path = [w for w, how in matches if how == "path"]
    candidates = by_path or [w for w, how in matches if how == "name"]
    if len(candidates) == 1:
        _set_window(row, candidates[0], "chain+project-path", "high" if by_path else "medium")
    elif not candidates:
        if not _set_workspace_only(row, windows, "chain+project-path", "no window title matched"):
            _unresolved(row, "project is open but no window title matches it")
    elif not _set_workspace_only(
        row, candidates, "chain+project-path", f"{len(candidates)} windows share the project name"
    ):
        _unresolved(
            row, f"{len(candidates)} windows share the project name on different workspaces"
        )


def _join_iterm2(row: dict[str, Any], windows: Sequence[Mapping[str, Any]]) -> None:
    named = [w for w in windows if row["name"] and w["title"] == row["name"]]
    if len(named) == 1:
        _set_window(row, named[0], "chain+title", "medium")
    elif len(windows) == 1:
        _set_window(row, windows[0], "chain+window", "low")
    elif not _set_workspace_only(row, windows, "chain+window", "iTerm2 window ambiguous"):
        _unresolved(row, "iTerm2: several windows and none matches the session name")


def _join_other(row: dict[str, Any], windows: Sequence[Mapping[str, Any]]) -> None:
    if len(windows) == 1:
        _set_window(row, windows[0], "chain+window", "low")
    elif not _set_workspace_only(row, windows, "chain+window", "host app has several windows"):
        _unresolved(row, "host app has several windows on different AeroSpace workspaces")


def join_rows(captured: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Join registry rows with windows. Pure: depends only on `captured`."""
    ps = {int(pid): (ppid, comm) for pid, (ppid, comm) in captured["ps"].items()}
    cmux_ids: Mapping[str, Mapping[str, str]] = captured.get("cmux_ids", {})
    windows: list[dict[str, Any]] = captured["windows"]
    home: str = captured["meta"]["home"]
    by_pid: dict[int, list[dict[str, Any]]] = {}
    for win in windows:
        by_pid.setdefault(int(win["app_pid"]), []).append(win)
    projects: dict[str, list[dict[str, Any]]] = {}
    for dirname, xml_text in captured.get("jetbrains", {}).items():
        try:
            projects.setdefault(product_stem(dirname), []).extend(
                parse_recent_projects(xml_text, home)
            )
        except ET.ParseError:
            continue

    rows: list[dict[str, Any]] = []
    for reg in sorted(captured["registry"], key=lambda r: int(r.get("pid") or 0)):
        pid = int(reg["pid"]) if str(reg.get("pid", "")).isdigit() else 0
        cwd = str(reg.get("cwd") or "").rstrip("/")
        row: dict[str, Any] = {
            "pid": pid,
            "name": reg.get("name") or "",
            "kind": reg.get("kind") or "",
            "entrypoint": reg.get("entrypoint") or "",
            "state": reg.get("status") or "",
            "version": reg.get("version") or "",
            "cwd": cwd,
            "project": os.path.basename(cwd),
            "host": "",
            "bundle_id": "",
            "window_id": None,
            "workspace": None,
            "via": "none",
            "confidence": "none",
            "cmux_ids": {},
            "note": "",
            "unresolved": None,
        }
        rows.append(row)
        if reg.get("_unreadable") or not pid:
            _unresolved(row, "registry row unreadable")
            continue
        if pid not in ps:
            _unresolved(row, "pid not running (stale registry row)")
            continue
        chain = parent_chain(pid, ps, set(by_pid))
        row["cmux_ids"] = _first_ids(chain, cmux_ids)
        if not chain or chain[-1] not in by_pid:
            _unresolved(row, "no ancestor process owns an AeroSpace window")
            continue
        host_windows = by_pid[chain[-1]]
        row["bundle_id"] = host_windows[0]["bundle_id"]
        row["host"] = host_kind(row["bundle_id"])
        if row["host"] == "cmux":
            _join_cmux(row, host_windows)
        elif row["host"] == "jetbrains":
            stem = jetbrains_stem_for(row["bundle_id"])
            _join_jetbrains(row, host_windows, projects.get(stem, []), home)
        elif row["host"] == "iterm2":
            _join_iterm2(row, host_windows)
        else:
            _join_other(row, host_windows)
    return rows


def _first_ids(chain: Sequence[int], ids: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    for pid in chain:
        found = ids.get(str(pid))
        if found:
            return dict(found)
    return {}


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-host counts: rows, window hits, AeroSpace-workspace hits, cmux-id hits."""
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        host = row["host"] or "unknown-host"
        stats = summary.setdefault(host, {"rows": 0, "window": 0, "workspace": 0, "cmux_ids": 0})
        stats["rows"] += 1
        stats["window"] += row["window_id"] is not None
        stats["workspace"] += row["workspace"] is not None
        stats["cmux_ids"] += bool(row["cmux_ids"])
    return summary


# --------------------------------------------------------------------------- rendering


def _cell(value: object, width: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["  ".join(_cell(title, width).ljust(width) for title, width in COLUMNS)]
    for row in rows:
        if row["unresolved"]:
            via = f"unresolved: {row['unresolved']}"
        else:
            via = f"{row['via']}/{row['confidence']}"
        values = (
            row["name"] or f"(pid {row['pid']})",
            f"{row['kind']}/{row['entrypoint']}",
            row["state"],
            row["project"],
            row["host"] or "-",
            row["window_id"] if row["window_id"] is not None else "-",
            row["workspace"] if row["workspace"] is not None else "-",
            via,
        )
        cells = [_cell(v, w).ljust(w) for v, (_, w) in zip(values, COLUMNS, strict=True)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def render_summary(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["", "host        rows  window  workspace  cmux-ids"]
    for host, st in sorted(summarize(rows).items()):
        counts = f"{st['rows']:>4}  {st['window']:>6}  {st['workspace']:>9}  {st['cmux_ids']:>8}"
        lines.append(f"{host:<11} {counts}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- live adapters


def _run(argv: Sequence[str], timeout: float = 10.0) -> str:
    done = subprocess.run(  # noqa: S603 - fixed read-only argv
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout


def _version(argv: Sequence[str]) -> str:
    try:
        return _run(argv).strip().splitlines()[0]
    except (OSError, RuntimeError, subprocess.TimeoutExpired, IndexError):
        return "unavailable"


def read_registry(home: str) -> list[dict[str, Any]]:
    directory = Path(home, ".claude", "sessions")
    rows: list[dict[str, Any]] = []
    if not directory.is_dir():
        return rows
    for name in sorted(os.listdir(directory)):
        if not REGISTRY_GLOB.fullmatch(name):
            continue
        try:
            data = json.loads((directory / name).read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
            rows.append(data)
        except (OSError, ValueError):
            rows.append({"pid": int(name.removesuffix(".json")), "_unreadable": True})
    return rows


def read_ps_and_cmux_ids() -> tuple[dict[int, tuple[int, str]], dict[str, dict[str, str]]]:
    """`ps` table plus the CMUX_* ids found in args. The raw args are never kept."""
    table = parse_ps_table(_run(["ps", "-A", "-o", "pid=,ppid=,comm="]))
    ids: dict[str, dict[str, str]] = {}
    for line in _run(["ps", "-A", "-o", "pid=,args="]).splitlines():
        head, _, args = line.strip().partition(" ")
        if head.isdigit() and "CMUX_" in args:
            found = extract_cmux_ids(args)
            if found:
                ids[head] = found
    return table, ids


def read_jetbrains(home: str) -> dict[str, str]:
    base = Path(home, "Library", "Application Support", "JetBrains")
    if not base.is_dir():
        return {}
    dirs = [d.name for d in base.iterdir() if (d / "options" / "recentProjects.xml").is_file()]
    xmls: dict[str, str] = {}
    for dirname in newest_product_dirs(dirs).values():
        xmls[dirname] = (base / dirname / "options" / "recentProjects.xml").read_text()
    return xmls


def read_live(home: str, argv: Sequence[str]) -> dict[str, Any]:
    ps, cmux_ids = read_ps_and_cmux_ids()
    windows_text = _run(
        ["aerospace", "list-windows", "--monitor", "all", "--json", "--format", WINDOW_FORMAT]
    )
    workspaces_text = _run(["aerospace", "list-workspaces", "--all", "--json"])
    registry = read_registry(home)
    return {
        "meta": {
            "date": dt.date.today().isoformat(),
            "home": home,
            "command_line": " ".join(argv),
            "aerospace": _version(["aerospace", "--version"]),
            "cmux": _version([CMUX_CLI, "--version"]),
            "claude_cli": _version(["claude", "--version"]),
            "claude_row_versions": sorted({str(r.get("version", "")) for r in registry}),
            "macos": _version(["sw_vers", "-productVersion"]),
        },
        "registry": registry,
        "windows_raw": windows_text,
        "windows": parse_windows(windows_text),
        "workspaces_raw": workspaces_text,
        "ps": {str(pid): list(value) for pid, value in ps.items()},
        "cmux_ids": cmux_ids,
        "jetbrains": read_jetbrains(home),
    }


# --------------------------------------------------------------------------- capture


def write_capture(directory: Path, captured: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(json.dumps(captured["meta"], indent=2) + "\n")
    (directory / "registry.json").write_text(json.dumps(captured["registry"], indent=2) + "\n")
    (directory / "windows.json").write_text(captured["windows_raw"])
    (directory / "workspaces.json").write_text(captured["workspaces_raw"])
    ps_lines = [f"{pid} {ppid} {comm}" for pid, (ppid, comm) in captured["ps"].items()]
    (directory / "ps.txt").write_text("\n".join(ps_lines) + "\n")
    (directory / "cmux_ids.json").write_text(json.dumps(captured["cmux_ids"], indent=2) + "\n")
    jb_dir = directory / "jetbrains"
    if jb_dir.exists():
        shutil.rmtree(jb_dir)
    jb_dir.mkdir()
    for dirname, text in captured["jetbrains"].items():
        (jb_dir / f"{dirname}.recentProjects.xml").write_text(text)


def read_capture(directory: Path) -> dict[str, Any]:
    windows_raw = (directory / "windows.json").read_text()
    jetbrains = {
        p.name.removesuffix(".recentProjects.xml"): p.read_text()
        for p in sorted((directory / "jetbrains").glob("*.recentProjects.xml"))
    }
    cmux_path = directory / "cmux_ids.json"
    return {
        "meta": json.loads((directory / "meta.json").read_text()),
        "registry": json.loads((directory / "registry.json").read_text()),
        "windows_raw": windows_raw,
        "windows": parse_windows(windows_raw),
        "workspaces_raw": (directory / "workspaces.json").read_text(),
        "ps": {
            str(pid): [int(ppid), comm]
            for pid, (ppid, comm) in parse_ps_table((directory / "ps.txt").read_text()).items()
        },
        "cmux_ids": json.loads(cmux_path.read_text()) if cmux_path.is_file() else {},
        "jetbrains": jetbrains,
    }


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="join_table.py",
        description=(
            "Join the live Claude registry with AeroSpace windows (read-only). Prints, for "
            "every registry row: name, kind/entrypoint, state, project, host app, window id, "
            "AeroSpace workspace, join via/confidence."
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--capture-out",
        metavar="DIR",
        type=Path,
        help="also write the raw inputs to DIR (use target/fixture-staging/join/<date>/)",
    )
    source.add_argument(
        "--from-capture",
        metavar="DIR",
        type=Path,
        help="run the join on a capture in DIR; nothing is read live",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(args_list)
    if args.from_capture:
        try:
            captured = read_capture(args.from_capture)
        except (OSError, ValueError, KeyError) as exc:
            print(f"join_table.py: cannot read capture: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            captured = read_live(os.path.expanduser("~"), ["join_table.py", *args_list])
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            print(f"join_table.py: live read failed: {exc}", file=sys.stderr)
            return 2
        if args.capture_out:
            write_capture(args.capture_out, captured)
    rows = join_rows(captured)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render_table(rows))
        print(render_summary(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
