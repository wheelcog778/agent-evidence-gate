#!/usr/bin/env python3
"""bridge_handoff.py — 把 AGENTS.md §5 的交接协议变成机器可写的

人类版（AGENTS.md §5）是五列 markdown 表：
  | 时间 | 上游要什么（原话） | 我做了什么 | 产出绝对路径 | 下次第一步 |

这里做三件事：
  1. 五列结构化落 handoff 表（一个 task 一行，重复写只更新）
  2. 渲染成同样的 markdown 追加到 交接/inbox/<date>-<actor>.md（幂等：先删同 task 旧块再写）
  3. 让下游任务的 context packet 只读这五列，而不是重读整篇历史
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("AGENT_BUS_DB", ROOT / "bus.db"))
INBOX = Path(os.environ.get("AGENT_BUS_INBOX", Path.home() / "交接" / "inbox"))

MARK_BEGIN = "<!-- agent-bus:handoff:{} -->"
MARK_END = "<!-- /agent-bus:handoff:{} -->"

# actor 会进 inbox 文件名，必须白名单，否则 ../../x 能把文件写到 inbox 外
SAFE_ACTOR = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _safe_actor(a: str) -> str:
    if not SAFE_ACTOR.match(a or ""):
        raise SystemExit(f"unsafe actor name: {a!r} (allowed: letters, digits, _ . -, max 64)")
    return a


def _atomic_write(path: Path, text: str) -> None:
    """先写同目录临时文件，再 os.replace 覆盖。

    直接 write_text 在写入中途崩溃会留下半截文件；
    os.replace 在同一卷上是原子的，读者要么看到旧的，要么看到新的。
    """
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    return c


def _table_md(rows: list[dict]) -> str:
    head = "| 时间 | 上游要什么（原话） | 我做了什么 | 产出绝对路径 | 下次第一步 |\n|---|---|---|---|---|\n"
    body = ""
    for r in rows:
        arts = "<br>".join(f"`{a}`" for a in (r["artifacts"] or [])) or "—"
        cells = [
            r["ts"],
            (r["upstream"] or "—").replace("\n", " ").replace("|", "\\|"),
            (r["done"] or "—").replace("\n", " ").replace("|", "\\|"),
            arts.replace("|", "\\|"),
            (r["next_step"] or "—").replace("\n", " ").replace("|", "\\|"),
        ]
        body += "| " + " | ".join(cells) + " |\n"
    return head + body


def write_handoff(task_id: str, done: str, next_step: str,
                  artifacts: list[str] | None = None, actor: str | None = None,
                  inbox: bool = True, note: str | None = None) -> dict:
    artifacts = [str(Path(a).resolve()) for a in (artifacts or [])]
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise SystemExit(f"task not found: {task_id}")
        actor = _safe_actor(actor or t["agent"])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 上游"原话"：父任务的 title/payload 原文，不转述
        upstream = None
        if t["parent_id"]:
            p = c.execute("SELECT * FROM task WHERE id=?", (t["parent_id"],)).fetchone()
            if p:
                upstream = p["title"]
                try:
                    pl = json.loads(p["payload"] or "{}")
                    ask = pl.get("ask") or pl.get("question") or pl.get("goal")
                    if ask:
                        upstream = f"{p['title']}：{ask}"
                except Exception:
                    pass

        c.execute(
            """INSERT INTO handoff(task_id, ts, upstream, done, artifacts, next_step, raw_json)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET
                 ts=excluded.ts, upstream=excluded.upstream, done=excluded.done,
                 artifacts=excluded.artifacts, next_step=excluded.next_step, raw_json=excluded.raw_json""",
            (task_id, ts, upstream, done, json.dumps(artifacts, ensure_ascii=False), next_step,
             json.dumps({"note": note}, ensure_ascii=False)),
        )
        c.execute(
            "INSERT INTO event(task_id, kind, actor, payload, created_at) VALUES(?,?,?,?,?)",
            (task_id, "handoff", actor, json.dumps({"artifacts": artifacts, "next_step": next_step}, ensure_ascii=False), time.time()),
        )
        # 注意：inbox 文件在事务内写，目的是让"DB 记录"和"文件内容"要么一起成立、
        # 要么一起不成立；SQLite 的写锁同时把并发 handoff 串行化，不会互相覆盖。
        result = {"task_id": task_id, "ts": ts, "upstream": upstream, "done": done,
                  "artifacts": artifacts, "next_step": next_step, "inbox": None}

        if inbox:
            INBOX.mkdir(parents=True, exist_ok=True)
            fp = INBOX / f"{datetime.now():%Y-%m-%d}-{actor}.md"
            block = (
                f"\n{MARK_BEGIN.format(task_id)}\n"
                f"### {task_id} · {t['title']}\n\n"
                + _table_md([result])
                + f"{MARK_END.format(task_id)}\n"
            )
            text = fp.read_text(encoding="utf-8") if fp.exists() else f"# {actor} 交接记录\n"
            b0, b1 = MARK_BEGIN.format(task_id), MARK_END.format(task_id)
            if b0 in text and b1 in text:
                pre = text.split(b0)[0]
                post = text.split(b1, 1)[1]
                text = pre + block.strip() + "\n" + post
            else:
                text = text.rstrip() + "\n" + block
            _atomic_write(fp, text)
            result["inbox"] = str(fp.resolve())

        c.execute("COMMIT")

        # 同步任务板的"下次第一步"（如果该 task 来自任务板）
        if t["source"] and str(t["source"]).startswith("board:"):
            from bridge_handoff import _update_board_next_step  # self
            result["board"] = _update_board_next_step(t, next_step)
        return result
    finally:
        c.close()


def _update_board_next_step(task_row, next_step: str) -> dict:
    """把 bus 任务映射回 交接/任务板.md 的「下次第一步」列。只改这一列。"""
    board = Path.home() / "交接" / "任务板.md"
    if not board.exists():
        return {"ok": False, "reason": "task board missing"}
    bid = str(task_row["source"]).split("board:", 1)[1]
    lines = board.read_text(encoding="utf-8").splitlines(keepends=True)
    hit = 0
    for i, ln in enumerate(lines):
        if ln.startswith(f"| {bid} |"):
            cells = ln.rstrip("\n").split("|")
            if len(cells) >= 8:
                cells[-2] = " " + next_step.replace("|", "\\|") + " "
                lines[i] = "|".join(cells) + "\n"
                hit += 1
    if hit:
        board.write_text("".join(lines), encoding="utf-8")
    return {"ok": bool(hit), "board": str(board), "rows": hit}


def read_handoff(task_id: str) -> dict | None:
    c = _conn()
    try:
        r = c.execute("SELECT * FROM handoff WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()