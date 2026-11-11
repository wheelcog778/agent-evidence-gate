#!/usr/bin/env python3
"""bridge_relay.py — run a task on a worker that cannot run commands.

Some agents are GUI applications: you type into a window, it writes files, and that
is the whole interface. It has no shell, no HTTP callback, no way to call `bus.py`.
Two things follow:

  1. it cannot claim a task, and it cannot submit evidence;
  2. if a human relays the task by hand, nothing proves the artifact is from *this*
     dispatch rather than the previous one.

This bridge closes both holes without touching bus.py. It takes the worker's place
in the state machine and keeps a timestamped snapshot of the output directory:

    dispatch   claim + start the task as the relay, render a self-contained brief,
               write a one-line wake message, snapshot the output directory
    collect    diff the snapshot, refuse files older than the dispatch, copy fresh
               files into evidence/, verify each copy's sha256, attach the primary
               one as evidence, mark the task done, and record a `relayed` event

The bus still refuses a task without evidence, still enforces the lease, and the
event log shows `relayed_by=<relay>` with `worker_ran_bus=false` — the artifact is
relayed, not self-certified.

Usage
----
    python bus.py submit --agent desktop --title "..." --payload '{"must_read":[...]}'
    python bridge_relay.py dispatch --id t_xxx            # claims, writes BRIEF.md + wake
    #   ... paste the first line of relay_inbox/t_xxx.md into the GUI ...
    python bridge_relay.py collect  --id t_xxx            # evidence + done
    python bridge_relay.py status   --id t_xxx
    python bridge_relay.py selftest                       # fully offline, temp DB

Layout (all relative to the repo unless overridden by env or flags):

    relay_out/<task-id>/BRIEF.md          the brief the worker reads
    relay_out/<task-id>/                  where the worker must write artifacts
    relay_out/<task-id>/evidence/         copies kept after the worker's files vanish
    relay_out/_state/<task-id>.json       dispatch time + snapshot
    relay_inbox/<task-id>.md              wake message (first line is the paste-able one)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
BUS = ROOT / "bus.py"
DEFAULT_RELAY = "relay"


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


DB_PATH = _env_path("AGENT_BUS_DB", ROOT / "bus.db")
OUT_ROOT = _env_path("RELAY_OUT", ROOT / "relay_out")
INBOX = _env_path("RELAY_INBOX", ROOT / "relay_inbox")

# Directories never scanned for artifacts: our own bookkeeping and VCS noise.
SKIP_DIRS = {".git", "__pycache__", "evidence", "_state", "node_modules"}

if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- bus plumbing

def bus_env() -> dict:
    return {**os.environ, "AGENT_BUS_DB": str(DB_PATH), "PYTHONIOENCODING": "utf-8"}


def bus(*args: str) -> dict:
    """Call bus.py and parse its JSON. Never re-implement state transitions here."""
    cp = subprocess.run([PY, str(BUS), *args], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env=bus_env(), timeout=120)
    if cp.returncode != 0:
        raise SystemExit(f"bus {args[0]} failed (rc={cp.returncode}): {cp.stderr.strip()[-400:]}")
    out = cp.stdout.strip()
    return json.loads(out) if out else {}


def task_of(tid: str) -> dict:
    return bus("status", "--id", tid)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- snapshots

def walk_files(base: Path) -> dict[str, tuple[float, int]]:
    found: dict[str, tuple[float, int]] = {}
    if not base.exists():
        return found
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            found[str(p.resolve())] = (st.st_mtime, st.st_size)
    return found


def snapshot(dirs: list[Path]) -> dict[str, list]:
    snap: dict[str, list] = {}
    for d in dirs:
        for path, (mtime, size) in walk_files(d).items():
            snap[path] = [mtime, size]
    return snap


def diff_fresh(dirs: list[Path], snap: dict[str, list], dispatch_time: float,
               exclude: set[str] | None = None) -> tuple[list[Path], list[Path]]:
    """Split new files into (fresh, stale).

    A file counts as fresh only if it is absent from the dispatch snapshot AND its
    mtime is strictly later than the dispatch. The second condition matters because
    a worker can copy an old artifact into the output directory to look productive.
    `exclude` carries the bridge's own scaffolding (BRIEF.md, wake message), which is
    written after dispatch_time and would otherwise look like a worker artifact.
    """
    exclude = exclude or set()
    fresh, stale = [], []
    for d in dirs:
        for path, (mtime, size) in walk_files(d).items():
            if path in exclude:
                continue
            before = snap.get(path)
            unchanged = before is not None and before[0] == mtime and before[1] == size
            if unchanged:
                continue
            if mtime > dispatch_time:
                fresh.append(Path(path))
            else:
                stale.append(Path(path))
    return sorted(fresh), sorted(stale)


# ---------------------------------------------------------------- state

def state_path(tid: str) -> Path:
    return OUT_ROOT / "_state" / f"{tid}.json"


def save_state(st: dict) -> None:
    p = state_path(st["task_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def load_state(tid: str) -> dict:
    p = state_path(tid)
    if not p.exists():
        raise SystemExit(f"no dispatch state for {tid}; run dispatch first")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- rendering

def render_brief(task: dict, out_dir: Path, relay: str) -> str:
    payload = task.get("payload") or {}
    must_read = payload.get("must_read") or payload.get("read") or []
    if isinstance(must_read, str):
        must_read = [must_read]
    suggested = payload.get("expected_artifact") or "artifact.md"
    reads = "\n".join(f"  - `{p}`" for p in must_read) or "  - (none declared: stay inside the output directory)"
    return f"""# Task brief (generated by bridge_relay.py — self-contained)

- task id: `{task['id']}`
- title: {task.get('title', '')}
- dispatched: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}
- relay: `{relay}` (it claims and submits for you; **do not** call bus.py yourself)

## Read only these paths (do not edit them)

{reads}

## Write your artifacts here (absolute path)

- directory: `{out_dir}`
- suggested file name: `{suggested}`

A sentence saying "done" is not evidence. A file on disk is. When you are finished,
report the file names back — nothing else.

## Never touch

- anything listed above as read-only;
- the bus database, the bus token, or any `bus.py` subcommand (`claim`, `evidence`, `done`, ...);
- directories outside `{out_dir}` unless this brief names them.

## Task payload (verbatim)

```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```
"""


def render_wake(tid: str, brief: Path, out_dir: Path) -> str:
    first = (f"Open and follow exactly this brief: {brief.as_posix()} — read only the paths it lists, "
             f"write artifacts into {out_dir.as_posix()}, then tell me the file names. "
             f"Do not submit anything.")
    return f"""{first}

---
(the line above is the whole message; paste it into the worker's window as-is)

- task id: `{tid}`
- brief: `{brief}`
- output directory: `{out_dir}`
- after the worker reports back: `python bridge_relay.py collect --id {tid}`
"""


# ---------------------------------------------------------------- dispatch

def do_dispatch(tid: str, worker: str | None, relay: str, lease: float,
                watch: list[Path]) -> tuple[int, dict]:
    task = task_of(tid)
    state = task.get("state")
    if state != "queued":
        return 1, {"error": "not_queued", "state": state,
                   "hint": "only a queued task can be dispatched"}

    worker = worker or task.get("agent")
    claimed = bus("claim", "--agent", worker, "--owner", relay, "--task", tid,
                  "--lease", str(lease)).get("claimed")
    if not claimed:
        return 1, {"error": "claim_failed", "why": "no matching queued task"}

    # claimed -> running: evidence may only be attached in claimed/running, and
    # `done` only accepts running.
    bus("start", "--id", tid, "--actor", relay)

    out_dir = OUT_ROOT / tid
    (out_dir).mkdir(parents=True, exist_ok=True)
    (INBOX).mkdir(parents=True, exist_ok=True)
    dispatch_time = time.time()

    dirs = [out_dir, *watch]
    snap = snapshot(dirs)

    brief = out_dir / "BRIEF.md"
    brief.write_text(render_brief(claimed, out_dir, relay), encoding="utf-8")

    wake = INBOX / f"{tid}.md"
    wake.write_text(render_wake(tid, brief, out_dir), encoding="utf-8")

    st = {
        "task_id": tid, "title": claimed.get("title", ""), "worker": worker, "relay": relay,
        "lease": lease, "dispatch_time": dispatch_time,
        "dispatch_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(dispatch_time)),
        "out_dir": str(out_dir), "brief": str(brief), "wake": str(wake),
        "candidate_dirs": [str(d) for d in dirs],
        "bridge_files": [str(brief.resolve()), str(wake.resolve())],
        "snapshot": snap,
    }
    save_state(st)
    return 0, {"dispatched": tid, "worker": worker, "relay": relay,
               "brief": str(brief), "wake": str(wake),
               "wake_first_line": wake.read_text(encoding="utf-8").splitlines()[0],
               "snapshot_count": len(snap), "lease_s": lease}


# ---------------------------------------------------------------- collect

def _copy_verified(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    a, b = sha256_file(src), sha256_file(dst)
    if a != b:
        raise SystemExit(f"copy mismatch: {src} -> {dst} ({a[:12]} != {b[:12]})")
    return b


def record_relayed(tid: str, relay: str, st: dict, sources: list[dict]) -> None:
    """Append a `relayed` event: who relayed, which files, and that the worker
    never touched the bus. The bus's own event log stays the primary record."""
    payload = {
        "relayed_by": relay,
        "worker": st.get("worker"),
        "worker_ran_bus": False,
        "dispatch_iso": st.get("dispatch_iso"),
        "sources": sources,
        "note": "artifact produced by a GUI worker and relayed by bridge_relay.py; not self-certified",
    }
    c = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    try:
        c.execute("PRAGMA busy_timeout=15000")
        c.execute(
            "INSERT INTO event(task_id, kind, actor, payload, created_at) VALUES(?,?,?,?,?)",
            (tid, "relayed", relay, json.dumps(payload, ensure_ascii=False), time.time()),
        )
    finally:
        c.close()


def do_collect(tid: str, verdict: str, accept_external: bool) -> tuple[int, dict]:
    st = load_state(tid)
    if st.get("collected"):
        return 1, {"error": "already_collected", "evidence": st.get("evidence")}

    out_dir = Path(st["out_dir"])
    dispatch_time = float(st["dispatch_time"])
    exclude = set(st.get("bridge_files") or [])
    watch = [Path(d) for d in st["candidate_dirs"] if Path(d) != out_dir]

    fresh, stale = diff_fresh([out_dir], st["snapshot"], dispatch_time, exclude)
    ext_fresh, _ = diff_fresh(watch, st["snapshot"], dispatch_time, exclude) if watch else ([], [])

    if not fresh and not ext_fresh:
        return 1, {"error": "no_fresh_artifact",
                   "stale_rejected": [str(p) for p in stale],
                   "hint": "the worker wrote nothing new after the dispatch"}
    if not fresh and not accept_external:
        return 1, {"error": "external_artifact_unconfirmed",
                   "external_fresh": [str(p) for p in ext_fresh],
                   "hint": "nothing landed in the output directory; re-run with --accept-external "
                           "only after checking those files yourself"}

    evidence_dir = out_dir / "evidence"
    copied, primary = [], None
    for src in fresh:
        dst = evidence_dir / src.name
        digest = _copy_verified(src, dst)
        entry = {"source_path": str(src), "evidence_path": str(dst), "sha256": digest,
                 "bytes": dst.stat().st_size}
        copied.append(entry)

    external_ignored: list[str] = []
    if ext_fresh and not accept_external:
        # A GUI worker often keeps its own notes next to its session; refusing the
        # whole run over them would stall the normal path. Record, do not accept.
        external_ignored = [str(p) for p in ext_fresh]
    for src in (ext_fresh if accept_external else []):
        src_dir = next((d for d in watch if str(src).startswith(str(d.resolve()))), None)
        dst = evidence_dir / f"external_{src.name}"
        digest = _copy_verified(src, dst)
        copied.append({"source_path": str(src), "evidence_path": str(dst), "sha256": digest,
                       "bytes": dst.stat().st_size, "external": True,
                       "source_dir": str(src_dir) if src_dir else None})

    # primary = the file the task asked for; otherwise the largest artifact
    wanted = (task_of(tid).get("payload") or {}).get("expected_artifact")
    if wanted:
        primary = next((e for e in copied if Path(e["source_path"]).name == wanted), None)
    primary = primary or max(copied, key=lambda e: e["bytes"])

    bus("evidence", "--id", tid, "--path", str(primary["evidence_path"]), "--actor", str(st["relay"]))
    bus("done", "--id", tid, "--actor", str(st["relay"]),
        "--verdict", verdict or "relayed from GUI worker")

    (evidence_dir / "manifest.json").write_text(
        json.dumps({"task_id": tid, "relay": st["relay"], "worker": st["worker"],
                    "dispatch_iso": st["dispatch_iso"], "artifacts": copied},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    record_relayed(tid, st["relay"], st, copied)

    st["collected"] = True
    st["evidence"] = copied
    save_state(st)
    return 0, {"collected": tid, "relay": st["relay"], "primary": primary["evidence_path"],
               "artifacts": copied, "stale_rejected": [str(p) for p in stale],
               "external_ignored": external_ignored}


# ---------------------------------------------------------------- status

def do_status(tid: str | None) -> tuple[int, dict]:
    if not tid:
        ids = sorted(p.stem for p in (OUT_ROOT / "_state").glob("*.json")) if (OUT_ROOT / "_state").exists() else []
        return 0, {"dispatched": ids}
    st = load_state(tid)
    fresh, stale = diff_fresh([Path(d) for d in st["candidate_dirs"]],
                              st["snapshot"], float(st["dispatch_time"]),
                              set(st.get("bridge_files") or []))
    t = task_of(tid)
    return 0, {"task_id": tid, "bus_state": t.get("state"), "lease_owner": t.get("lease_owner"),
               "verdict": t.get("verdict"), "evidence_path": t.get("evidence_path"),
               "fresh_now": [str(p) for p in fresh], "stale_seen": [str(p) for p in stale],
               "collected": bool(st.get("collected"))}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile

    global DB_PATH, OUT_ROOT, INBOX
    tmp = Path(tempfile.mkdtemp(prefix="relay-selftest-"))
    DB_PATH = tmp / "bus.db"
    OUT_ROOT = tmp / "relay_out"
    INBOX = tmp / "relay_inbox"
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def worker_write(p: Path, text: str = "result\n", mtime: float | None = None) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))

    bus("init")
    task = bus("submit", "--agent", "gui-worker", "--title", "selftest task",
               "--payload", json.dumps({"must_read": [str(BUS)], "expected_artifact": "report.md"})
               ).get("task", {})
    tid = task.get("id")
    chk("submit creates a queued task", task.get("state") == "queued", str(task.get("state")))

    rc, info = do_dispatch(tid, None, DEFAULT_RELAY, 600, [])
    chk("dispatch claims and starts", rc == 0 and task_of(tid).get("state") == "running")
    chk("wake first line names the brief", "BRIEF.md" in info.get("wake_first_line", ""))
    chk("brief exists on disk", Path(info.get("brief", "")).is_file())

    rc2, info2 = do_dispatch(tid, None, DEFAULT_RELAY, 600, [])
    chk("second dispatch is refused", rc2 == 1 and info2.get("error") in ("not_queued", "claim_failed"),
        str(info2.get("error")))

    rc3, info3 = do_collect(tid, "", False)
    chk("collect refuses when nothing new exists", rc3 == 1 and info3.get("error") == "no_fresh_artifact",
        str(info3.get("error")))

    # stale: a file with mtime before the dispatch must not count
    st = load_state(tid)
    worker_write(Path(st["out_dir"]) / "old.md", "old", mtime=float(st["dispatch_time"]) - 60)
    rc4, info4 = do_collect(tid, "", False)
    chk("collect refuses a pre-dispatch file", rc4 == 1 and info4.get("error") == "no_fresh_artifact",
        str(info4.get("error")))

    (Path(st["out_dir"]) / "old.md").unlink()
    worker_write(Path(st["out_dir"]) / "report.md", "the deliverable\n")
    rc5, info5 = do_collect(tid, "relayed ok", False)
    chk("collect submits fresh artifact", rc5 == 0, str(info5)[:120])
    after = task_of(tid)
    chk("task is done", after.get("state") == "done", str(after.get("state")))
    ev = Path(after.get("evidence_path", ""))
    chk("evidence is a copy under evidence/", ev.is_file() and ev.parent.name == "evidence", str(ev))
    chk("evidence hash matches the worker file",
        ev.is_file() and sha256_file(ev) == sha256_file(Path(st["out_dir"]) / "report.md"))
    kinds = [e["kind"] for e in after.get("events", [])]
    chk("relayed event recorded", "relayed" in kinds, ",".join(kinds))
    chk("worker never touched the bus", json.loads(
        next(e["payload"] for e in after["events"] if e["kind"] == "relayed")).get("worker_ran_bus") is False)

    # external artifact: refused by default, accepted on request
    t2 = bus("submit", "--agent", "gui-worker", "--title", "external case",
             "--payload", json.dumps({"expected_artifact": "out.md"})).get("task", {})
    tid2 = t2["id"]
    ext = tmp / "elsewhere"
    ext.mkdir()
    do_dispatch(tid2, None, DEFAULT_RELAY, 600, [ext])
    worker_write(ext / "notes.md", "notes\n")
    rc6, info6 = do_collect(tid2, "", False)
    chk("external-only is refused by default", rc6 == 1 and info6.get("error") == "external_artifact_unconfirmed",
        str(info6.get("error")))
    worker_write(Path(OUT_ROOT / tid2) / "out.md", "primary\n")
    rc7, info7 = do_collect(tid2, "", True)
    chk("--accept-external collects both", rc7 == 0 and len(info7.get("artifacts", [])) == 2,
        str(len(info7.get("artifacts", []))))

    # local artifact + external noise, no flag: the local one is accepted, noise is recorded only
    t3 = bus("submit", "--agent", "gui-worker", "--title", "external ignored",
             "--payload", json.dumps({"expected_artifact": "out.md"})).get("task", {})
    tid3 = t3["id"]
    ext2 = tmp / "elsewhere2"
    ext2.mkdir()
    do_dispatch(tid3, None, DEFAULT_RELAY, 600, [ext2])
    worker_write(Path(OUT_ROOT / tid3) / "out.md", "primary\n")
    worker_write(ext2 / "memory.md", "worker notes\n")
    rc8, info8 = do_collect(tid3, "", False)
    chk("local artifact wins, external is ignored not refused",
        rc8 == 0 and len(info8.get("artifacts", [])) == 1 and len(info8.get("external_ignored", [])) == 1,
        str(info8.get("error")))

    ok = sum(1 for _, o, _ in checks if o)
    for name, o, detail in checks:
        print(f"  [{'PASS' if o else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not o else ""))
    print(f"=== {ok}/{len(checks)} PASS ===")
    print(f"temp dir: {tmp}")
    return 0 if ok == len(checks) else 1


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bridge_relay", description="relay tasks to a worker that cannot run commands")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dispatch", help="claim+start, render brief and wake message, snapshot the output dir")
    p.add_argument("--id", required=True)
    p.add_argument("--worker", help="defaults to the task's agent field")
    p.add_argument("--relay", default=DEFAULT_RELAY)
    p.add_argument("--lease", type=float, default=7200,
                   help="there is no heartbeat for a GUI worker: pick a lease longer than the job")
    p.add_argument("--watch", action="append", default=[], help="extra directory to watch (repeatable)")

    p = sub.add_parser("collect", help="diff snapshot, copy evidence, attach it, mark done")
    p.add_argument("--id", required=True)
    p.add_argument("--verdict", default="")
    p.add_argument("--accept-external", action="store_true",
                   help="also accept fresh files outside the output directory (you checked them)")

    p = sub.add_parser("status", help="what is dispatched, what is new")
    p.add_argument("--id")

    sub.add_parser("selftest", help="offline checks against a temporary database")

    a = ap.parse_args(argv)
    if a.cmd == "dispatch":
        rc, out = do_dispatch(a.id, a.worker, a.relay, a.lease, [Path(w) for w in a.watch])
    elif a.cmd == "collect":
        rc, out = do_collect(a.id, a.verdict, a.accept_external)
    elif a.cmd == "status":
        rc, out = do_status(a.id)
    else:
        return selftest()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())