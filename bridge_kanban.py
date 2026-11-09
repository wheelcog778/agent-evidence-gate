#!/usr/bin/env python3
"""bridge_kanban.py — 和 Hermes crew（kanban）对接

不替代 kanban，只把它当成"任务来源 + 审计归档"。

  mirror  读 kanban.db 里未结束的卡，导入 bus（幂等：client_nonce=kanban:<id>）
  push    把 bus 的终态回写成 kanban 评论（默认只打印命令，--exec 才真写）
  diff    两边状态对照

关键约束：kanban 的 body 经常几 KB，直接塞进 payload 会把 agent 上下文撑爆。
mirror 只取：标题 + 前 1200 字（按字节截断）+ workspace_path。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUS_DB = Path(os.environ.get("AGENT_BUS_DB", ROOT / "bus.db"))
KANBAN_DB = Path(os.environ.get(
    "KANBAN_DB", Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "kanban.db"))
BODY_LIMIT = 1200

OPEN_STATES = ("triage", "todo", "ready", "running", "blocked", "review")
STATE_MAP = {
    "triage": "queued", "todo": "queued", "ready": "queued",
    "running": "running", "blocked": "blocked_on_human", "review": "blocked_on_human",
}


def _clip(s: str, n: int) -> str:
    b = (s or "").encode("utf-8")[:n]
    while b:
        try:
            return b.decode("utf-8") + "…"
        except UnicodeDecodeError:
            b = b[:-1]
    return ""


def mirror(agent_filter: str | None, dry: bool) -> dict:
    if not KANBAN_DB.exists():
        raise SystemExit(f"kanban db not found: {KANBAN_DB}")
    k = sqlite3.connect(f"file:{KANBAN_DB.as_posix()}?mode=ro", uri=True)
    k.row_factory = sqlite3.Row
    rows = k.execute(
        f"SELECT * FROM tasks WHERE status IN ({','.join('?'*len(OPEN_STATES))})", OPEN_STATES
    ).fetchall()
    k.close()

    b = sqlite3.connect(str(BUS_DB), timeout=15, isolation_level=None)
    b.row_factory = sqlite3.Row
    created, skipped = [], []
    try:
        b.execute("BEGIN IMMEDIATE")
        for r in rows:
            agent = agent_filter or r["assignee"] or "unassigned"
            nonce = f"kanban:{r['id']}"
            if b.execute("SELECT 1 FROM task WHERE client_nonce=?", (nonce,)).fetchone():
                skipped.append(r["id"])
                continue
            payload = {
                "ask": _clip(r["body"] or r["title"], BODY_LIMIT),
                "workspace": r["workspace_path"],
                "kanban_status": r["status"],
                "skills": r["skills"],
            }
            if dry:
                created.append({"kanban": r["id"], "agent": agent, "title": r["title"][:80]})
                continue
            tid = "t_" + os.urandom(6).hex()
            b.execute(
                """INSERT INTO task(id,parent_id,root_id,depth,agent,title,payload,state,client_nonce,
                   priority,created_at,updated_at,max_attempts,source)
                   VALUES(?,NULL,?,0,?,?,?,?,?,?,?,?,3,?)""",
                (tid, tid, agent, r["title"][:200], json.dumps(payload, ensure_ascii=False),
                 STATE_MAP.get(r["status"], "queued"), nonce, 1,
                 float(r["created_at"] or 0), float(r["created_at"] or 0), f"kanban:{r['id']}"),
            )
            b.execute(
                "INSERT INTO event(task_id,kind,actor,payload,created_at) VALUES(?,?,?,?,?)",
                (tid, "mirrored", "bridge", json.dumps({"kanban": r["id"]}, ensure_ascii=False),
                 float(r["created_at"] or 0)),
            )
            created.append({"bus": tid, "kanban": r["id"], "agent": agent, "bytes": len(payload["ask"].encode())})
        b.execute("COMMIT")
    finally:
        b.close()
    return {"kanban_open": len(rows), "created": created, "skipped_existing": skipped, "dry": dry}


def diff() -> dict:
    k = sqlite3.connect(f"file:{KANBAN_DB.as_posix()}?mode=ro", uri=True)
    k.row_factory = sqlite3.Row
    kn = {r["id"]: r["status"] for r in k.execute("SELECT id,status FROM tasks")}
    k.close()
    b = sqlite3.connect(f"file:{BUS_DB.as_posix()}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    out = []
    for r in b.execute("SELECT * FROM task WHERE source LIKE 'kanban:%'"):
        kid = r["source"].split(":", 1)[1]
        out.append({"bus": r["id"], "bus_state": r["state"], "kanban_id": kid,
                    "kanban_status": kn.get(kid, "MISSING"), "title": r["title"][:60]})
    b.close()
    return {"rows": out, "count": len(out)}


def _reserve(bus_id: str, kid: str) -> bool:
    """事务内抢占一条待推送记录；抢到返回 True。

    两个进程同时跑 push --exec 时，只有一个能插入 reserved 行，
    另一个直接跳过——否则同一条卡片会被评论两次。
    """
    c = sqlite3.connect(str(BUS_DB), timeout=15, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            """INSERT INTO event(task_id, kind, actor, payload, created_at)
               SELECT ?, 'pushed_reserved', 'bridge', ?, ?
               WHERE NOT EXISTS (
                   SELECT 1 FROM event WHERE task_id=? AND kind IN ('pushed','pushed_reserved')
               )""",
            (bus_id, json.dumps({"kanban": kid}, ensure_ascii=False), time.time(), bus_id),
        )
        c.execute("COMMIT")
        return cur.rowcount > 0
    finally:
        c.close()


def _settle(bus_id: str, ok: bool) -> None:
    """成功 -> 改成 pushed；失败 -> 删掉 reserved，允许下次重试。"""
    c = sqlite3.connect(str(BUS_DB), timeout=15, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        if ok:
            c.execute("UPDATE event SET kind='pushed' WHERE task_id=? AND kind='pushed_reserved'", (bus_id,))
        else:
            c.execute("DELETE FROM event WHERE task_id=? AND kind='pushed_reserved'", (bus_id,))
        c.execute("COMMIT")
    finally:
        c.close()


def push(exec_: bool) -> dict:
    b = sqlite3.connect(f"file:{BUS_DB.as_posix()}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    rows = b.execute(
        """SELECT * FROM task WHERE source LIKE 'kanban:%' AND state IN ('done','failed','dead')
           AND id NOT IN (SELECT task_id FROM event WHERE kind IN ('pushed','pushed_reserved'))"""
    ).fetchall()
    b.close()

    commands, results = [], []
    for r in rows:
        kid = r["source"].split(":", 1)[1]
        msg = f"[agent-bus] {r['id']} -> {r['state']}；verdict={r['verdict'] or '-'}；证据={r['evidence_path'] or '-'}"
        commands.append((r["id"], kid, msg))

    if not exec_:
        return {"pending": len(commands), "executed": False,
                "commands": [f'hermes kanban comment {k} "{m}"' for _, k, m in commands], "results": []}

    for bus_id, kid, msg in commands:
        if not _reserve(bus_id, kid):
            results.append({"bus": bus_id, "kanban": kid, "skipped": "already reserved by another process"})
            continue
        try:
            cp = subprocess.run(["hermes", "kanban", "comment", kid, msg],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            ok = cp.returncode == 0
            results.append({"bus": bus_id, "kanban": kid, "code": cp.returncode,
                            "err": "" if ok else cp.stderr[-300:]})
        except Exception as e:
            ok = False
            results.append({"bus": bus_id, "kanban": kid, "code": 99,
                            "err": f"{type(e).__name__}: {e}"})
        _settle(bus_id, ok)

    return {"pending": len(commands), "executed": True, "commands": [], "results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("mirror", "push", "diff"))
    ap.add_argument("--agent", help="覆盖 assignee，统一导入给某个 agent")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--exec", dest="exec_", action="store_true")
    a = ap.parse_args()
    if a.cmd == "mirror":
        print(json.dumps(mirror(a.agent, a.dry), ensure_ascii=False, indent=2))
    elif a.cmd == "diff":
        print(json.dumps(diff(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(push(a.exec_), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()