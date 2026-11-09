#!/usr/bin/env python3
"""context.py — 预算式上下文包（pi 风格的"少吃上下文"）

原则：agent 不该看到全量历史。它只该看到
  1) 这张任务卡要什么
  2) 它自己的岗位说明（截断）
  3) 上游交回的结论（截断，不含上游的完整思考过程）
  4) 全局硬规则（截断）

预算按字节计，超了按优先级丢；丢掉的记在 dropped 里，不许静默。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("AGENT_BUS_DB", ROOT / "bus.db"))
BRIEF_DIR = Path(os.environ.get("AGENT_BUS_BRIEFS", ROOT / "profiles"))
RULES_FILE = Path(os.environ.get("AGENT_BUS_RULES", ROOT / "RULES.md"))

DEFAULT_BUDGET = 8192
SLOTS = [
    # (name, 目标大小上限, 越界时是否可裁)
    ("task", 3072, True),
    ("upstream", 2048, True),
    ("brief", 1536, True),
    ("rules", 1024, True),
]


def _clip(text: str, limit: int) -> tuple[str, int]:
    """按字节截断到合法 UTF-8 边界。返回 (text, 原始字节数)。

    截断标记算在额度内，否则返回值会超过 limit，"硬上限"就成了假话。
    """
    if limit <= 0:
        return "", len(text.encode("utf-8"))
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, len(raw)
    suffix = "\n…[truncated]"
    sb = suffix.encode("utf-8")
    budget = max(0, limit - len(sb))
    cut = raw[:budget]
    while cut:
        try:
            return cut.decode("utf-8") + suffix, len(raw)
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", len(raw)


SAFE_AGENT = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _brief(agent: str) -> str:
    # agent 名直接拼文件名：不校验就能用 ../../x 读到 profiles 之外的 md
    if not SAFE_AGENT.match(agent or ""):
        return f"你是 {agent}。只做这一张卡上的事，做完交证据。"
    p = (BRIEF_DIR / f"{agent}.md")
    try:
        p = p.resolve()
        p.relative_to(BRIEF_DIR.resolve())
    except (ValueError, OSError):
        return f"你是 {agent}。只做这一张卡上的事，做完交证据。"
    if p.exists() and p.is_file():
        return p.read_text(encoding="utf-8")
    return f"你是 {agent}。只做这一张卡上的事，做完交证据。"


def _rules() -> str:
    if not RULES_FILE.exists():
        return ""
    lines = RULES_FILE.read_text(encoding="utf-8").splitlines()
    keep = [ln for ln in lines if ln.startswith("#") or ln.startswith("-") or ln.startswith("*")]
    return "\n".join(keep)


def _row(c: sqlite3.Connection, tid: str):
    return c.execute("SELECT * FROM task WHERE id=?", (tid,)).fetchone()


def ancestors_bytes(c: sqlite3.Connection, tid: str) -> int:
    """全量历史口径。

    如果一个 agent 不走 packet，它为了接住这张卡，实际必须读：
      - 每个祖先的 payload
      - 每个祖先的 handoff 五列
      - 每个祖先交回的产物文件本身（这才是不用 packet 时最大的开销）
    这个数字才是 packet 的对照组。
    """
    total = 0
    cur = tid
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        r = _row(c, cur)
        if not r:
            break
        total += len((r["payload"] or "").encode("utf-8"))
        h = c.execute("SELECT * FROM handoff WHERE task_id=?", (cur,)).fetchone()
        if h:
            total += len(json.dumps(dict(h), ensure_ascii=False).encode("utf-8"))
            try:
                for f in json.loads(h["artifacts"] or "[]"):
                    p = Path(f)
                    if p.is_file():
                        total += p.stat().st_size
            except Exception:
                pass
        if r["evidence_path"]:
            p = Path(r["evidence_path"])
            if p.is_file():
                total += p.stat().st_size
        cur = r["parent_id"]
    return total


def build_packet(tid: str, budget: int = DEFAULT_BUDGET, db: Path | None = None) -> dict:
    dbp = db or DB_PATH
    c = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        # 整个 packet 必须来自同一个快照：上游 handoff 与祖先链条不能跨两次提交
        c.execute("BEGIN")
        t = _row(c, tid)
        if not t:
            raise SystemExit(f"task not found: {tid}")

        slots: dict[str, str] = {}
        dropped: list[str] = []

        # 1) 任务核心（永不被完全裁掉）
        payload = json.loads(t["payload"] or "{}")
        task_txt = json.dumps(
            {"id": t["id"], "title": t["title"], "agent": t["agent"], "payload": payload},
            ensure_ascii=False, indent=1,
        )
        slots["task"], _ = _clip(task_txt, SLOTS[0][1])

        # 2) 上游 handoff（只要结论，不要过程）
        up = ""
        upstream_files: list[str] = []
        if t["parent_id"]:
            h = c.execute("SELECT * FROM handoff WHERE task_id=?", (t["parent_id"],)).fetchone()
            if h:
                try:
                    upstream_files = json.loads(h["artifacts"] or "[]")
                except Exception:
                    upstream_files = []
                up = (
                    f"上游要什么：{h['upstream'] or ''}\n"
                    f"上游做了什么：{h['done'] or ''}\n"
                    f"上游产出：{h['artifacts'] or '[]'}\n"
                    f"上游的下一步：{h['next_step'] or ''}"
                )
            else:
                p = _row(c, t["parent_id"])
                up = f"上游任务（无 handoff）：{p['title'] if p else 'missing'}"
        slots["upstream"], _ = _clip(up, SLOTS[1][1])

        # 3) 岗位说明
        slots["brief"], _ = _clip(_brief(t["agent"]), SLOTS[2][1])

        # 4) 全局硬规则
        slots["rules"], _ = _clip(_rules(), SLOTS[3][1])

        # 预算装配：task 优先，其余按序，超预算的槽整体降级
        remaining = budget
        used: dict[str, int] = {}
        final: dict[str, str] = {}
        for name, _cap, _cullable in SLOTS:
            text = slots.get(name, "")
            if not text:
                used[name] = 0
                final[name] = ""
                continue
            b = len(text.encode("utf-8"))
            if b <= remaining:
                final[name] = text
                used[name] = b
                remaining -= b
            else:
                if name == "task":
                    # 预算不足时不许强行给 256B，那会突破硬上限
                    final[name], b = _clip(text, max(0, remaining))
                    used[name] = len(final[name].encode("utf-8"))
                    remaining -= used[name]
                    if not final[name]:
                        dropped.append(name)
                else:
                    dropped.append(name)
                    final[name] = ""
                    used[name] = 0

        full = ancestors_bytes(c, tid)
        used_total = sum(used.values())
        c.execute("COMMIT")
        # 根任务没有"历史"可比：它的 payload 就是全部输入，负比率是假的
        comparable = t["parent_id"] is not None
        return {
            "task_id": tid,
            "agent": t["agent"],
            "parent_id": t["parent_id"],
            "upstream_files": upstream_files,
            "packet": final,
            "budget_bytes": budget,
            "used_bytes": used_total,
            "slot_bytes": used,
            "dropped": dropped,
            "full_history_bytes": full if comparable else None,
            "saved_bytes": max(0, full - used_total) if comparable else None,
            "saved_ratio": (round(1 - used_total / full, 3) if full else None) if comparable else None,
            "comparable": comparable,
        }
    finally:
        c.close()


def render_prompt(packet: dict) -> str:
    p = packet["packet"]
    parts = []
    if p.get("brief"):
        parts.append("# 你的岗位\n" + p["brief"])
    if p.get("rules"):
        parts.append("# 硬规则\n" + p["rules"])
    if p.get("upstream"):
        parts.append("# 上游交来的\n" + p["upstream"])
    parts.append("# 你这张卡\n" + p["task"])
    parts.append(
        "\n# 交付要求\n"
        "只写这一张卡要的东西。把产物路径、跑过的命令、预期输出写进 manifest.json。\n"
        "不要把过程复述一遍，也不要读与本卡无关的历史。"
    )
    return "\n\n".join(parts)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    ap.add_argument("--prompt", action="store_true")
    a = ap.parse_args()
    pk = build_packet(a.id, a.budget)
    print(render_prompt(pk) if a.prompt else json.dumps(pk, ensure_ascii=False, indent=2))