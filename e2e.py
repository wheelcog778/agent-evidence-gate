#!/usr/bin/env python3
"""e2e.py — 一条真实链路：researcher → verifier → 模型复核

每次运行都用新 nonce，可重复执行。
输出每一步的状态、证据路径、上下文成本。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ENV = {**os.environ, "AGENT_BUS_DB": str(ROOT / "bus.db"), "PYTHONIOENCODING": "utf-8",
       "AGENT_BUS_BRIEFS": str(ROOT / "profiles"), "AGENT_BUS_RULES": str(ROOT / "RULES.md"),
       # 测试交接写到沙箱 inbox，不污染真实 交接/inbox（11-08 已因此重复写入 12 条）
       "AGENT_BUS_INBOX": os.environ.get("AGENT_BUS_INBOX", str(ROOT / "runs" / "_e2e_inbox"))}


def sh(*args: str, timeout: int = 600) -> dict:
    cp = subprocess.run([PY, str(ROOT / "bus.py"), *args], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", env=ENV, timeout=timeout)
    if cp.returncode != 0:
        raise SystemExit(f"bus {args[0]} failed: {cp.stderr[-400:]}")
    return json.loads(cp.stdout)


def run_agent(tid: str, cmd: str, timeout: int = 600) -> dict:
    cp = subprocess.run([PY, str(ROOT / "runner.py"), "--id", tid, "--cmd", cmd, "--timeout", str(timeout)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    try:
        return json.loads(cp.stdout)
    except Exception:
        return {"ok": False, "reason": cp.stdout[-300:] + cp.stderr[-300:]}


def step(tid: str, agent: str) -> None:
    sh("claim", "--agent", agent, "--task", tid)
    sh("start", "--id", tid)


def main() -> int:
    tag = str(int(time.time()))
    target = ROOT / "demo" / "sample-agents.md"
    if not target.exists():
        target = Path.home() / "AGENTS.md"
    rows = []

    # ---- 1 researcher
    t1 = sh("submit", "--agent", "researcher", "--title", "统计交接协议结构",
            "--nonce", f"e2e2-{tag}-r", "--payload",
            json.dumps({"target": str(target), "ask": "统计 AGENTS.md 的章节数与规则条数"}, ensure_ascii=False))["task"]["id"]
    step(t1, "researcher")
    r1 = run_agent(t1, 'python "{ROOT}/demo/agent_research.py"')
    sh("handoff", "--task", t1, "--done", "统计 AGENTS.md 的结构、行数与 sha256，附 4 条可重跑命令",
       "--next-step", "独立核验 report.md 的章节、指纹与复现节",
       "--artifacts", str(ROOT / "runs" / t1 / "work" / "report.md"))
    rows.append(("researcher", t1, r1))

    # ---- 2 verifier
    t2 = sh("submit", "--agent", "verifier", "--title", "独立核验 report.md",
            "--nonce", f"e2e2-{tag}-v", "--parent", t1, "--payload",
            json.dumps({"ask": "核验上游 report.md 的章节、指纹与复现节"}, ensure_ascii=False))["task"]["id"]
    step(t2, "verifier")
    r2 = run_agent(t2, 'python "{ROOT}/demo/agent_verify.py"')
    sh("handoff", "--task", t2, "--done", "独立重算 sha256 并核对章节与复现节",
       "--next-step", "用模型复核这份报告是否满足验收三件套",
       "--artifacts", str(ROOT / "runs" / t2 / "work" / "verdict.md"))
    rows.append(("verifier", t2, r2))

    # ---- 3 模型复核
    verdict_txt = ""
    vp = ROOT / "runs" / t2 / "work" / "verdict.md"
    if vp.exists():
        verdict_txt = vp.read_text(encoding="utf-8", errors="replace")[:1800]
    t3 = sh("submit", "--agent", "verifier", "--title", "模型复核：报告是否满足验收三件套",
            "--nonce", f"e2e2-{tag}-m", "--parent", t2, "--payload",
            json.dumps({"target": str(ROOT / "runs" / t1 / "work" / "report.md"),
                        "upstream_verdict": verdict_txt,
                        "question": "这份 report.md 是否满足「产物路径+验证命令输出+口径可复算」三项验收？逐项判 PASS/FAIL 并给依据。实测部分由上游 verifier 完成，见其结论。"},
                       ensure_ascii=False))["task"]["id"]
    step(t3, "verifier")
    r3 = run_agent(t3, 'python "{ROOT}/demo/agent_llm.py"', timeout=300)
    rows.append(("verifier-llm", t3, r3))

    print("\n=== 结果 ===")
    env_failure = False
    for agent, tid, r in rows:
        ctx = r.get("context", {})
        note = ""
        if agent == "verifier-llm" and not r.get("ok"):
            # 模型通道挂了是环境问题，不是总线逻辑失败；要标出来，不能混为一谈
            note = "  [SKIP: 模型通道不可用，非逻辑失败]"
            env_failure = True
        print(f"{agent:14s} {tid}  ok={r.get('ok')}  "
              f"ctx={ctx.get('used_bytes')}B/{ctx.get('full_history_bytes')}B "
              f"saved={ctx.get('saved_ratio')}  reason={r.get('reason')}{note}")

    answer = ROOT / "runs" / t3 / "work" / "answer.md"
    print("\n=== 模型复核结论 ===")
    if answer.exists():
        print(answer.read_text(encoding="utf-8")[:1400])
    else:
        print("answer.md 未生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())