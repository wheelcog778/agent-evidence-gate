#!/usr/bin/env python3
"""agent-bus: 轻量任务总线（Task Bus）

设计约束（对应 multi-agent-architecture §1/§2）：
  - 状态在磁盘，不在 agent 上下文里。上下文是缓存，日志是真相。
  - 每个入站任务带 client_nonce UNIQUE -> 重投不会双跑。
  - parent_id 是因果链，也是递归深度限制。
  - deadline 到期即失败，不无限挂着。
  - evidence_path 必须存在且 hash 对得上，否则不许 done。
  - 审批只拦四类：outbound / spend / delete / permission；超时即拒。
  - 租约（lease）让崩溃的 worker 可被回收，attempts 有上限。

CLI 用法见 --help。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("AGENT_BUS_DB", ROOT / "bus.db"))

APPROVAL_KINDS = ("outbound", "spend", "delete", "permission")
STATES = ("queued", "claimed", "running", "blocked_on_human", "done", "failed", "cancelled", "dead")
TERMINAL = ("done", "failed", "cancelled", "dead")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS task (
    id              TEXT PRIMARY KEY,
    parent_id       TEXT,
    root_id         TEXT,
    depth           INTEGER NOT NULL DEFAULT 0,
    agent           TEXT NOT NULL,
    title           TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL DEFAULT 'queued',
    client_nonce    TEXT UNIQUE,
    priority        INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    deadline        REAL,
    lease_owner     TEXT,
    lease_until     REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    evidence_path   TEXT,
    evidence_sha256 TEXT,
    verdict         TEXT,
    require_approval TEXT,
    approval_state  TEXT,
    approval_reason TEXT,
    context_bytes   INTEGER,
    source          TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_state ON task(state, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_task_parent ON task(parent_id);

CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    actor      TEXT,
    payload    TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_task ON event(task_id, id);

CREATE TABLE IF NOT EXISTS handoff (
    task_id     TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    upstream    TEXT,          -- 上游要什么（原话）
    done        TEXT,          -- 我做了什么
    artifacts   TEXT,          -- 产出绝对路径（JSON 数组）
    next_step   TEXT,          -- 下次第一步
    raw_json    TEXT
);
"""


# ---------------------------------------------------------------- 基础

def now() -> float:
    return time.time()


def conn(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        c = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    else:
        c = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = conn()
    c.executescript(SCHEMA)
    c.close()
    print(f"initialized {DB_PATH}")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def emit_event(c: sqlite3.Connection, task_id: str, kind: str, actor: str | None, payload: dict | None = None) -> None:
    c.execute(
        "INSERT INTO event(task_id, kind, actor, payload, created_at) VALUES(?,?,?,?,?)",
        (task_id, kind, actor, json.dumps(payload or {}, ensure_ascii=False), now()),
    )


def row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("payload", "require_approval"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- 提交

def cmd_submit(a: argparse.Namespace) -> None:
    init_if_needed()
    payload = {}
    if a.payload_file:
        payload = json.loads(Path(a.payload_file).read_text(encoding="utf-8"))
    elif a.payload:
        payload = json.loads(a.payload)

    nonce = a.nonce or str(uuid.uuid4())
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            """INSERT OR IGNORE INTO task
               (id,parent_id,root_id,depth,agent,title,payload,state,client_nonce,priority,
                created_at,updated_at,deadline,max_attempts,require_approval,source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "t_" + uuid.uuid4().hex[:12],
                a.parent, a.parent, 0,
                a.agent, a.title, json.dumps(payload, ensure_ascii=False),
                "blocked_on_human" if a.require_approval else "queued",
                nonce, a.priority, now(), now(),
                (now() + a.deadline) if a.deadline else None,
                a.max_attempts, a.require_approval, a.source,
            ),
        )
        if cur.rowcount == 0:
            existing = c.execute("SELECT * FROM task WHERE client_nonce=?", (nonce,)).fetchone()
            c.execute("COMMIT")
            print(json.dumps({"duplicate": True, "task": row_to_dict(existing)}, ensure_ascii=False))
            return

        tid = c.execute("SELECT id FROM task WHERE client_nonce=?", (nonce,)).fetchone()["id"]

        depth = 0
        root = tid
        if a.parent:
            p = c.execute("SELECT root_id, depth FROM task WHERE id=?", (a.parent,)).fetchone()
            if not p:
                c.execute("ROLLBACK")
                die(f"parent {a.parent} not found")
            depth = p["depth"] + 1
            root = p["root_id"] or a.parent
            if depth > a.max_depth:
                c.execute("ROLLBACK")
                die(f"depth {depth} > max_depth {a.max_depth} (root {root})")
        # 根任务的 root_id 必须指向自己，否则祖先链统计会漏掉它
        c.execute("UPDATE task SET root_id=?, depth=? WHERE id=?", (root, depth, tid))

        if a.require_approval:
            bad = [k for k in a.require_approval.split(",") if k.strip() not in APPROVAL_KINDS]
            if bad:
                c.execute("ROLLBACK")
                die(f"unknown approval kinds {bad}; allowed {APPROVAL_KINDS}")
            c.execute(
                "UPDATE task SET approval_state='pending', approval_reason=? WHERE id=?",
                (a.approval_reason or "", tid),
            )

        emit_event(c, tid, "submitted", a.agent, {"title": a.title, "nonce": nonce})
        c.execute("COMMIT")
        row = c.execute("SELECT * FROM task WHERE id=?", (tid,)).fetchone()
        print(json.dumps({"duplicate": False, "task": row_to_dict(row)}, ensure_ascii=False))
    finally:
        c.close()


def init_if_needed() -> None:
    if not DB_PATH.exists():
        init_db()


# ---------------------------------------------------------------- 认领（原子）

def cmd_claim(a: argparse.Namespace) -> None:
    init_if_needed()
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        q = """SELECT id FROM task
               WHERE state='queued' AND agent=? AND (deadline IS NULL OR deadline > ?)
               ORDER BY priority DESC, created_at ASC LIMIT 1"""
        params = [a.agent, now()]
        if a.task:
            # 显式 id 也要过同样的条件：别的 agent 不能抢，过期任务不能抢
            q = ("SELECT id FROM task WHERE id=? AND state='queued' AND agent=? "
                 "AND (deadline IS NULL OR deadline > ?)")
            params = [a.task, a.agent, now()]
        r = c.execute(q, params).fetchone()
        if not r:
            c.execute("COMMIT")
            print(json.dumps({"claimed": None}, ensure_ascii=False))
            return
        tid = r["id"]
        c.execute(
            """UPDATE task SET state='claimed', lease_owner=?, lease_until=?, attempts=attempts+1,
                   updated_at=? WHERE id=? AND state='queued'""",
            (a.owner or a.agent, now() + a.lease, now(), tid),
        )
        emit_event(c, tid, "claimed", a.owner or a.agent, {"lease_s": a.lease})
        c.execute("COMMIT")
        row = c.execute("SELECT * FROM task WHERE id=?", (tid,)).fetchone()
        print(json.dumps({"claimed": row_to_dict(row)}, ensure_ascii=False))
    finally:
        c.close()


def cmd_start(a: argparse.Namespace) -> None:
    _transition(a, "running", ("claimed",), "started")


def cmd_evidence(a: argparse.Namespace) -> None:
    init_if_needed()
    c = conn()
    try:
        p = Path(a.path)
        if not p.exists():
            die(f"evidence not found: {p}")
        # 证据必须是普通文件：目录能骗过"存在性"，但 hash 是空的
        if not p.is_file():
            die(f"evidence must be a regular file, got {'dir' if p.is_dir() else 'special'}: {p}")
        digest = sha256_file(p) if p.is_file() else ""
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT state FROM task WHERE id=?", (a.id,)).fetchone()
        if not t:
            c.execute("ROLLBACK"); die("task not found")
        # 证据只能在执行中登记：done/failed 之后不许再改，否则终态可被追溯篡改
        if t["state"] not in ("claimed", "running"):
            c.execute("ROLLBACK"); die(f"cannot attach evidence in state {t['state']}; must be claimed/running")
        c.execute(
            "UPDATE task SET evidence_path=?, evidence_sha256=?, updated_at=? WHERE id=?",
            (str(p.resolve()), digest, now(), a.id),
        )
        emit_event(c, a.id, "evidence", a.actor, {"path": str(p.resolve()), "sha256": digest})
        c.execute("COMMIT")
        print(json.dumps({"ok": True, "path": str(p.resolve()), "sha256": digest, "bytes": p.stat().st_size}, ensure_ascii=False))
    finally:
        c.close()


def cmd_done(a: argparse.Namespace) -> None:
    init_if_needed()
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM task WHERE id=?", (a.id,)).fetchone()
        if not t:
            c.execute("ROLLBACK"); die("task not found")
        if t["state"] in TERMINAL:
            c.execute("ROLLBACK"); die(f"task already {t['state']}")
        if t["state"] != "running":
            c.execute("ROLLBACK"); die(f"cannot finish from state {t['state']}; must be running")
        # 证据门
        if not t["evidence_path"]:
            c.execute("ROLLBACK"); die("evidence gate: no evidence_path; run `evidence` first")
        ep = Path(t["evidence_path"])
        if not ep.is_file():
            c.execute("ROLLBACK"); die(f"evidence gate: not a regular file: {ep}")
        if not t["evidence_sha256"]:
            c.execute("ROLLBACK"); die("evidence gate: no sha256 recorded")
        if sha256_file(ep) != t["evidence_sha256"]:
            c.execute("ROLLBACK"); die("evidence gate: sha256 mismatch (file changed after evidence)")
        # 审批门
        if t["require_approval"] and t["approval_state"] != "approved":
            c.execute("ROLLBACK"); die(f"approval gate: state={t['approval_state']}")
        # 期限门
        if t["deadline"] and now() > t["deadline"]:
            c.execute("ROLLBACK"); die("deadline gate: expired; use fail")

        c.execute(
            "UPDATE task SET state='done', verdict=?, updated_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
            (a.verdict, now(), a.id),
        )
        emit_event(c, a.id, "done", a.actor, {"verdict": a.verdict, "evidence": t["evidence_path"]})
        c.execute("COMMIT")
        print(json.dumps({"ok": True, "id": a.id, "state": "done"}, ensure_ascii=False))
    finally:
        c.close()


def cmd_fail(a: argparse.Namespace) -> None:
    init_if_needed()
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM task WHERE id=?", (a.id,)).fetchone()
        if not t:
            c.execute("ROLLBACK"); die("task not found")
        if t["state"] in TERMINAL:
            c.execute("ROLLBACK"); die(f"task already {t['state']}")
        if t["state"] not in ("claimed", "running", "blocked_on_human"):
            c.execute("ROLLBACK"); die(f"cannot fail from state {t['state']}")
        # 审批未过不许重排：否则 fail --retry 就是绕过审批的后门
        if t["require_approval"] and t["approval_state"] != "approved" and a.retry:
            c.execute("ROLLBACK"); die("cannot retry: approval still pending")
        # attempts 用尽后无条件 dead，--retry 不能覆盖
        exhausted = t["attempts"] >= t["max_attempts"]
        new_state = "dead" if exhausted else ("queued" if a.retry else "failed")
        c.execute(
            "UPDATE task SET state=?, verdict=?, updated_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
            (new_state, a.reason, now(), a.id),
        )
        emit_event(c, a.id, "fail", a.actor,
                   {"reason": a.reason, "attempts": t["attempts"], "state": new_state, "retry": a.retry})
        c.execute("COMMIT")
        print(json.dumps({"ok": True, "id": a.id, "state": new_state, "attempts": t["attempts"]}, ensure_ascii=False))
    finally:
        c.close()


def cmd_block(a: argparse.Namespace) -> None:
    init_if_needed()
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM task WHERE id=?", (a.id,)).fetchone()
        if not t:
            c.execute("ROLLBACK"); die("task not found")
        # 终态不许被 block，否则 approve 能把 done/dead 复活成 queued
        if t["state"] in TERMINAL:
            c.execute("ROLLBACK"); die(f"cannot block a terminal task ({t['state']})")
        if t["state"] == "blocked_on_human":
            c.execute("ROLLBACK"); die("already blocked_on_human")
        c.execute(
            "UPDATE task SET state='blocked_on_human', approval_reason=?, updated_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
            (a.reason, now(), a.id),
        )
        emit_event(c, a.id, "blocked", a.actor, {"reason": a.reason})
        c.execute("COMMIT")
        print(json.dumps({"ok": True, "id": a.id, "state": "blocked_on_human"}, ensure_ascii=False))
    finally:
        c.close()


def cmd_approve(a: argparse.Namespace) -> None:
    init_if_needed()
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM task WHERE id=?", (a.id,)).fetchone()
        if not t:
            c.execute("ROLLBACK"); die("task not found")
        if t["state"] != "blocked_on_human":
            c.execute("ROLLBACK"); die(f"not blocked_on_human (is {t['state']})")
        # 只处理审批流：普通 block（无 require_approval）不能靠 approve 放回队列
        if not t["require_approval"]:
            c.execute("ROLLBACK"); die("not an approval task (no require_approval); use fail --retry to requeue")
        if t["approval_state"] != "pending":
            c.execute("ROLLBACK"); die(f"approval_state={t['approval_state']}, expected pending")
        new_state = "queued" if a.decision == "approve" else "cancelled"
        c.execute(
            "UPDATE task SET state=?, approval_state=?, updated_at=? WHERE id=?",
            (new_state, "approved" if a.decision == "approve" else "denied", now(), a.id),
        )
        emit_event(c, a.id, "approval", a.actor, {"decision": a.decision, "note": a.note})
        c.execute("COMMIT")
        print(json.dumps({"ok": True, "id": a.id, "state": new_state}, ensure_ascii=False))
    finally:
        c.close()


def cmd_reap(a: argparse.Namespace) -> None:
    """回收过期租约与过期 deadline。"""
    init_if_needed()
    c = conn()
    out = {"requeued": [], "dead": [], "timed_out": []}
    try:
        c.execute("BEGIN IMMEDIATE")
        ts = now()
        for t in c.execute(
            "SELECT * FROM task WHERE state IN ('claimed','running') AND lease_until IS NOT NULL AND lease_until < ?", (ts,)
        ).fetchall():
            if t["attempts"] >= t["max_attempts"]:
                c.execute("UPDATE task SET state='dead', verdict='lease_expired_max_attempts', updated_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?", (ts, t["id"]))
                out["dead"].append(t["id"])
            else:
                c.execute("UPDATE task SET state='queued', updated_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?", (ts, t["id"]))
                out["requeued"].append(t["id"])
            emit_event(c, t["id"], "reaped", "reaper", {"attempts": t["attempts"]})
        for t in c.execute(
            "SELECT * FROM task WHERE state IN ('queued','claimed','running','blocked_on_human') "
            "AND deadline IS NOT NULL AND deadline < ?", (ts,)
        ).fetchall():
            c.execute("UPDATE task SET state='failed', verdict='deadline_exceeded', updated_at=? WHERE id=?", (ts, t["id"]))
            emit_event(c, t["id"], "deadline", "reaper", {})
            out["timed_out"].append(t["id"])
        c.execute("COMMIT")
        print(json.dumps(out, ensure_ascii=False))
    finally:
        c.close()


def _transition(a: argparse.Namespace, target: str, allowed: tuple, kind: str) -> None:
    init_if_needed()
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM task WHERE id=?", (a.id,)).fetchone()
        if not t:
            c.execute("ROLLBACK"); die("task not found")
        if t["state"] not in allowed:
            c.execute("ROLLBACK"); die(f"cannot go {t['state']} -> {target}")
        if t["lease_until"] and t["lease_until"] < now() and target != "queued":
            c.execute("ROLLBACK"); die("lease expired; run reap")
        c.execute("UPDATE task SET state=?, updated_at=? WHERE id=?", (target, now(), a.id))
        emit_event(c, a.id, kind, a.actor, {})
        c.execute("COMMIT")
        print(json.dumps({"ok": True, "id": a.id, "state": target}, ensure_ascii=False))
    finally:
        c.close()


# ---------------------------------------------------------------- 查询

def cmd_status(a: argparse.Namespace) -> None:
    init_if_needed()
    c = conn(readonly=True)
    try:
        # 一个观测结果必须来自同一个快照：否则 task 字段和 events 可能分属两次提交
        c.execute("BEGIN")
        if a.id:
            t = c.execute("SELECT * FROM task WHERE id=?", (a.id,)).fetchone()
            if not t:
                die("not found")
            d = row_to_dict(t)
            d["events"] = [dict(e) for e in c.execute("SELECT kind,actor,payload,created_at FROM event WHERE task_id=? ORDER BY id", (a.id,))]
            c.execute("COMMIT")
            print(json.dumps(d, ensure_ascii=False, indent=2))
            return
        rows = c.execute(
            """SELECT id,agent,title,state,attempts,evidence_path,verdict,priority,created_at
               FROM task WHERE state NOT IN ('done','failed','cancelled','dead')
               ORDER BY priority DESC, created_at"""
        ).fetchall()
        counts = {r["state"]: r["n"] for r in c.execute("SELECT state, count(*) n FROM task GROUP BY state")}
        c.execute("COMMIT")
        print(json.dumps({"open": [dict(r) for r in rows], "counts": counts, "db": str(DB_PATH)}, ensure_ascii=False, indent=2))
    finally:
        c.close()


def cmd_packet(a: argparse.Namespace) -> None:
    """输出该任务的 context packet（含预算与裁剪记录）。"""
    from context import build_packet
    p = build_packet(a.id, budget=a.budget)
    print(json.dumps(p, ensure_ascii=False, indent=2))


def cmd_handoff(a: argparse.Namespace) -> None:
    from bridge_handoff import write_handoff
    print(json.dumps(write_handoff(a.task, a.done, a.next_step, a.artifacts), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- CLI

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="bus", description="agent-bus: 轻量任务总线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.set_defaults(fn=lambda a: init_db())

    s = sub.add_parser("submit")
    s.add_argument("--agent", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--payload")
    s.add_argument("--payload-file")
    s.add_argument("--nonce")
    s.add_argument("--parent")
    s.add_argument("--priority", type=int, default=1)
    s.add_argument("--deadline", type=float, help="seconds from now")
    s.add_argument("--max-attempts", type=int, default=3)
    s.add_argument("--max-depth", type=int, default=4)
    s.add_argument("--require-approval", help="comma list: outbound,spend,delete,permission")
    s.add_argument("--approval-reason")
    s.add_argument("--source")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("claim")
    s.add_argument("--agent", required=True)
    s.add_argument("--owner")
    s.add_argument("--lease", type=float, default=900)
    s.add_argument("--task")
    s.set_defaults(fn=cmd_claim)

    for name, fn in (("start", cmd_start), ("done", cmd_done), ("fail", cmd_fail), ("block", cmd_block)):
        s = sub.add_parser(name)
        s.add_argument("--id", required=True)
        s.add_argument("--actor")
        if name == "done":
            s.add_argument("--verdict", default="")
        if name == "fail":
            s.add_argument("--reason", required=True)
            s.add_argument("--retry", action="store_true")
        if name == "block":
            s.add_argument("--reason", required=True)
        s.set_defaults(fn=fn)

    s = sub.add_parser("evidence")
    s.add_argument("--id", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--actor")
    s.set_defaults(fn=cmd_evidence)

    s = sub.add_parser("approve")
    s.add_argument("--id", required=True)
    s.add_argument("--decision", choices=("approve", "deny"), required=True)
    s.add_argument("--note")
    s.add_argument("--actor", default="human")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("reap")
    s.set_defaults(fn=cmd_reap)

    s = sub.add_parser("status")
    s.add_argument("--id")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("packet")
    s.add_argument("--id", required=True)
    s.add_argument("--budget", type=int, default=8192)
    s.set_defaults(fn=cmd_packet)

    s = sub.add_parser("handoff")
    s.add_argument("--task", required=True)
    s.add_argument("--done", required=True)
    s.add_argument("--next-step", required=True)
    s.add_argument("--artifacts", nargs="*", default=[])
    s.set_defaults(fn=cmd_handoff)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()