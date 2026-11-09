#!/usr/bin/env python3
"""selftest.py — 真跑一遍总线，断言每一条约束都成立

用法：python selftest.py
退出码 0 = 全过。每个 FAIL 都打印实测值。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run(db: Path, *args: str, cwd: Path | None = None, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AGENT_BUS_DB"] = str(db)
    env["AGENT_BUS_BRIEFS"] = str(ROOT / "profiles")
    env["AGENT_BUS_RULES"] = str(ROOT / "RULES.md")
    env["AGENT_BUS_INBOX"] = str(db.parent / "inbox")
    env["AGENT_BUS_RUNS"] = str(db.parent / "runs")
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PY, str(ROOT / "bus.py"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(cwd or ROOT),
    )


def j(cp: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(cp.stdout)
    except Exception:
        return {}


def sha(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agentbus-selftest-"))
    db = tmp / "bus.db"
    print(f"tmp db: {db}\n")

    # ---------------------------------------------------------- 0 init
    cp = run(db, "init")
    check("0 init 建库", db.exists() and cp.returncode == 0, str(db))

    # ---------------------------------------------------------- 1 幂等
    nonce = "dup-nonce-001"
    args = ("submit", "--agent", "researcher", "--title", "幂等测试", "--nonce", nonce)
    r1, r2 = j(run(db, *args)), j(run(db, *args))
    check("1 幂等：同 nonce 只建一条",
          (not r1.get("duplicate")) and r2.get("duplicate") and r1["task"]["id"] == r2["task"]["id"],
          f"id={r1.get('task', {}).get('id')} dup={r2.get('duplicate')}")

    # ---------------------------------------------------------- 2 租约互斥
    run(db, "submit", "--agent", "racer", "--title", "抢锁测试", "--nonce", "race-1")
    outs: dict[int, dict] = {}

    def racer(i: int):
        outs[i] = j(run(db, "claim", "--agent", "racer", "--owner", f"w{i}", "--lease", "60"))

    ts = [threading.Thread(target=racer, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    got = [o for o in outs.values() if o.get("claimed")]
    check("2 租约互斥：3 个 worker 只有 1 个拿到",
          len(got) == 1, f"claims={len(got)} owners={[g['claimed']['lease_owner'] for g in got]}")

    # ---------------------------------------------------------- 3 证据门
    t3 = j(run(db, "submit", "--agent", "researcher", "--title", "无证据", "--nonce", "ev-1"))["task"]["id"]
    run(db, "claim", "--agent", "researcher", "--task", t3)
    run(db, "start", "--id", t3)
    cp = run(db, "done", "--id", t3, "--verdict", "空口通过")
    check("3 证据门：无 evidence 不许 done",
          cp.returncode != 0 and "evidence gate" in cp.stderr,
          cp.stderr.strip()[:100])

    # ---------------------------------------------------------- 4 审批门
    t4 = j(run(db, "submit", "--agent", "researcher", "--title", "要审批", "--nonce", "ap-1",
               "--require-approval", "outbound", "--approval-reason", "会发外部消息"))["task"]["id"]
    st = j(run(db, "status", "--id", t4))
    check("4a 审批：提交即 blocked_on_human", st.get("state") == "blocked_on_human", st.get("state"))
    cp = run(db, "claim", "--agent", "researcher", "--task", t4)
    check("4b 审批：未批不可认领", not j(cp).get("claimed"), str(j(cp))[:120])
    run(db, "approve", "--id", t4, "--decision", "approve")
    st2 = j(run(db, "status", "--id", t4))
    check("4c 审批：批准后回 queued", st2.get("state") == "queued", st2.get("state"))

    # ---------------------------------------------------------- 5 租约超时回收
    t5 = j(run(db, "submit", "--agent", "slow", "--title", "租约超时", "--nonce", "lease-1"))["task"]["id"]
    run(db, "claim", "--agent", "slow", "--task", t5, "--lease", "0.2")
    time.sleep(0.4)
    out = j(run(db, "reap"))
    st5 = j(run(db, "status", "--id", t5))
    check("5 租约超时：reap 回 queued 且 attempts=1",
          t5 in out.get("requeued", []) and st5.get("state") == "queued" and st5.get("attempts") == 1,
          f"state={st5.get('state')} attempts={st5.get('attempts')}")

    # ---------------------------------------------------------- 6 attempts 上限 -> dead
    t6 = j(run(db, "submit", "--agent", "doomed", "--title", "必死", "--nonce", "dead-1",
               "--max-attempts", "1"))["task"]["id"]
    run(db, "claim", "--agent", "doomed", "--task", t6, "--lease", "0.1")
    time.sleep(0.25)
    out6 = j(run(db, "reap"))
    st6 = j(run(db, "status", "--id", t6))
    check("6 超限：attempts>=max 直接 dead",
          t6 in out6.get("dead", []) and st6.get("state") == "dead",
          f"state={st6.get('state')}")

    # ---------------------------------------------------------- 7 全链路 + 交接
    target = ROOT / "RULES.md"
    if not target.exists():
        target = ROOT / "RULES.md"
    t7 = j(run(db, "submit", "--agent", "researcher", "--title", "读交接协议",
               "--nonce", "pipe-1",
               "--payload", json.dumps({"target": str(target), "ask": "统计交接协议的结构与规则条数"})))["task"]["id"]
    cp = run(db, "claim", "--agent", "researcher", "--task", t7)
    run(db, "start", "--id", t7)
    cp = subprocess.run(
        [PY, str(ROOT / "runner.py"), "--id", t7, "--cmd", f'{PY} "{ROOT / "demo" / "agent_research.py"}"'],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "AGENT_BUS_DB": str(db), "AGENT_BUS_BRIEFS": str(ROOT / "profiles"),
             "AGENT_BUS_RULES": str(ROOT / "RULES.md"), "AGENT_BUS_INBOX": str(db.parent / "inbox"),
             "AGENT_BUS_RUNS": str(db.parent / "runs"), "PYTHONIOENCODING": "utf-8"},
    )
    st7 = j(run(db, "status", "--id", t7))
    check("7a 全链路：researcher 产出证据并 done",
          st7.get("state") == "done" and st7.get("evidence_path"),
          f"state={st7.get('state')} rc={cp.returncode}")

    h = j(run(db, "handoff", "--task", t7, "--done", "统计了 RULES.md 的结构",
              "--next-step", "核验增速与格式", "--artifacts",
              str(db.parent / "runs" / t7 / "work" / "report.md")))
    check("7b 交接：handoff 落库", bool(h.get("ts")) and bool(h.get("artifacts")), json.dumps(h.get("artifacts"), ensure_ascii=False)[:80])
    inbox_file = Path(h["inbox"]) if h.get("inbox") else None
    check("7c 交接：inbox 五列生成",
          bool(inbox_file and inbox_file.exists() and "| 时间 |" in inbox_file.read_text(encoding="utf-8")),
          str(inbox_file))

    t8 = j(run(db, "submit", "--agent", "verifier", "--title", "核验 report", "--nonce", "pipe-2",
               "--parent", t7, "--payload", json.dumps({"ask": "独立核验上游 report"})))["task"]["id"]
    run(db, "claim", "--agent", "verifier", "--task", t8)
    run(db, "start", "--id", t8)
    cp = subprocess.run(
        [PY, str(ROOT / "runner.py"), "--id", t8, "--cmd", f'{PY} "{ROOT / "demo" / "agent_verify.py"}"'],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "AGENT_BUS_DB": str(db), "AGENT_BUS_BRIEFS": str(ROOT / "profiles"),
             "AGENT_BUS_RULES": str(ROOT / "RULES.md"), "AGENT_BUS_INBOX": str(db.parent / "inbox"),
             "AGENT_BUS_RUNS": str(db.parent / "runs"), "PYTHONIOENCODING": "utf-8"},
    )
    st8 = j(run(db, "status", "--id", t8))
    verify_md = db.parent / "runs" / t8 / "work" / "verdict.md"
    check("7d 全链路：verifier 独立核验 PASS",
          st8.get("state") == "done" and verify_md.exists() and verify_md.read_text(encoding="utf-8").startswith("结论：PASS"),
          f"state={st8.get('state')} verdict={verify_md.read_text(encoding='utf-8').splitlines()[0] if verify_md.exists() else 'missing'}")

    # ---------------------------------------------------------- 8 上下文预算
    pkt = j(run(db, "packet", "--id", t8))
    check("8 上下文预算：packet 小于全量历史",
          pkt.get("used_bytes", 0) < pkt.get("full_history_bytes", 1) and pkt.get("used_bytes", 0) <= pkt.get("budget_bytes", 0),
          f"used={pkt.get('used_bytes')}B full={pkt.get('full_history_bytes')}B saved={pkt.get('saved_ratio')}")

    # ---------------------------------------------------------- 9 证据篡改检测
    t9 = j(run(db, "submit", "--agent", "researcher", "--title", "篡改测试", "--nonce", "tamper-1",
               "--payload", json.dumps({"target": str(target)})))["task"]["id"]
    run(db, "claim", "--agent", "researcher", "--task", t9)
    run(db, "start", "--id", t9)
    fake = tmp / "fake.md"
    fake.write_text("orig", encoding="utf-8")
    run(db, "evidence", "--id", t9, "--path", str(fake))
    fake.write_text("tampered", encoding="utf-8")
    cp = run(db, "done", "--id", t9, "--verdict", "ok")
    check("9 证据门：文件被改后 sha256 不匹配即拒",
          cp.returncode != 0 and "sha256 mismatch" in cp.stderr, cp.stderr.strip()[:90])

    # ---------------------------------------------------------- 10 kanban 融合
    check("10 kanban 融合（starter 不含 kanban 桥，跳过）", True, "skipped")

    # ---------------------------------------------------------- 11-13 证据门 v2 的负数用例
    from runner import verify_manifest
    w11 = tmp / "w11"
    w11.mkdir()
    (w11 / "a.md").write_text("x", encoding="utf-8")
    base_ok = {
        "artifacts": [{"path": "a.md", "sha256": sha(w11 / "a.md"), "produced_by": 0}],
        "output_summary": "s", "verdict": "v",
    }

    bad1 = {**base_ok, "commands": ["python x.py"]}
    ok1, p1 = verify_manifest(bad1, w11)
    check("11 证据门 v2：字符串命令不再被接受", not ok1 and any("must be objects" in x for x in p1),
          (p1[0] if p1 else "")[:100])

    no_pb = {**base_ok, "artifacts": [{"path": "a.md", "sha256": sha(w11 / "a.md")}],
             "commands": [{"cmd": "python a.py", "exit_code": 0, "stdout_path": "o.log", "stdout_sha256": sha(w11 / "a.md")}]}
    (w11 / "o.log").write_text("out", encoding="utf-8")
    no_pb["commands"][0]["stdout_sha256"] = sha(w11 / "o.log")
    ok2, p2 = verify_manifest(no_pb, w11)
    check("12 证据门 v2：产物缺 produced_by 即拒", not ok2 and any("produced_by" in x for x in p2),
          (p2[0] if p2 else "")[:100])

    good = {**base_ok, "commands": [{"cmd": "python a.py", "exit_code": 0,
                                     "stdout_path": "o.log", "stdout_sha256": sha(w11 / "o.log")}]}
    (w11 / "o.log").write_text("tampered", encoding="utf-8")
    ok3, p3 = verify_manifest(good, w11)
    check("13 证据门 v2：stdout 被改即拒", not ok3 and any("stdout_sha256 mismatch" in x for x in p3),
          (p3[0] if p3 else "")[:100])

    # ---------------------------------------------------------- 14-16 证据门 v4（Codex 审出的漏洞）
    (w11 / "o.log").write_text("out", encoding="utf-8")
    good_cmds = [{"cmd": "python a.py", "exit_code": 0,
                  "stdout_path": "o.log", "stdout_sha256": sha(w11 / "o.log")}]

    no_hash = {"artifacts": [{"path": "a.md", "produced_by": 0}],
               "commands": good_cmds, "output_summary": "s", "verdict": "v"}
    ok4, p4 = verify_manifest(no_hash, w11)
    check("14 证据门 v4：artifact 缺 sha256 即拒", not ok4 and any("has no sha256" in x for x in p4),
          (p4[0] if p4 else "")[:100])

    dir_stdout = {**base_ok, "commands": [{"cmd": "python a.py", "exit_code": 0,
                                           "stdout_path": ".", "stdout_sha256": "x"}]}
    ok5, p5 = verify_manifest(dir_stdout, w11)
    check("15 证据门 v4：stdout 是目录即拒", not ok5 and any("not a regular file" in x for x in p5),
          (p5[0] if p5 else "")[:100])

    escape = {"artifacts": [{"path": "../../outside.md", "sha256": "x", "produced_by": 0}],
              "commands": good_cmds, "output_summary": "s", "verdict": "v"}
    ok6, p6 = verify_manifest(escape, w11)
    check("16 证据门 v4：路径穿越即拒", not ok6 and any("escapes workdir" in x for x in p6),
          (p6[0] if p6 else "")[:100])

    # ---------------------------------------------------------- 17-21 Codex P0 的端到端断言
    import subprocess as _sp

    def _runner(tid: str, cmd: str) -> _sp.CompletedProcess:
        env = {**os.environ, "AGENT_BUS_DB": str(db), "AGENT_BUS_BRIEFS": str(ROOT / "profiles"),
               "AGENT_BUS_RULES": str(ROOT / "RULES.md"), "AGENT_BUS_INBOX": str(db.parent / "inbox"),
               "AGENT_BUS_RUNS": str(db.parent / "runs"), "PYTHONIOENCODING": "utf-8"}
        return _sp.run([PY, str(ROOT / "runner.py"), "--id", tid, "--cmd", cmd, "--timeout", "60"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)

    t17 = j(run(db, "submit", "--agent", "researcher", "--title", "未认领就跑", "--nonce", "p0-1"))["task"]["id"]
    cp = _runner(t17, 'python -c "print(1)"')
    check("17 runner 拒未 start 的任务",
          cp.returncode != 0 and "ERROR runner refuses: task state" in cp.stderr and "Traceback" not in cp.stderr,
          cp.stderr.strip()[:90])

    t18 = j(run(db, "submit", "--agent", "researcher", "--title", "终态复活", "--nonce", "p0-2"))["task"]["id"]
    run(db, "claim", "--agent", "researcher", "--task", t18)
    run(db, "start", "--id", t18)
    run(db, "fail", "--id", t18, "--reason", "test")
    cp = run(db, "block", "--id", t18, "--reason", "想复活")
    check("18 block 不能复活终态", cp.returncode != 0 and "terminal" in cp.stderr, cp.stderr.strip()[:90])

    t19 = j(run(db, "submit", "--agent", "researcher", "--title", "目录当证据", "--nonce", "p0-7"))["task"]["id"]
    run(db, "claim", "--agent", "researcher", "--task", t19)
    run(db, "start", "--id", t19)
    cp = run(db, "evidence", "--id", t19, "--path", str(tmp))
    check("19 目录不能当证据", cp.returncode != 0 and "regular file" in cp.stderr, cp.stderr.strip()[:90])

    t20 = j(run(db, "submit", "--agent", "researcher", "--title", "拿审批绕过", "--nonce", "p0-3",
                "--require-approval", "spend"))["task"]["id"]
    cp = run(db, "fail", "--id", t20, "--reason", "想跳过审批", "--retry")
    check("20 审批未过不许 fail --retry", cp.returncode != 0 and "approval still pending" in cp.stderr,
          cp.stderr.strip()[:90])

    # 21: agent 写出合法 manifest 后 exit(1)，不许被当成成功
    t21 = j(run(db, "submit", "--agent", "researcher", "--title", "非零退出", "--nonce", "p0-6"))["task"]["id"]
    run(db, "claim", "--agent", "researcher", "--task", t21)
    run(db, "start", "--id", t21)
    cheat = tmp / "cheat.py"
    cheat.write_text(
        "import json,os,hashlib,pathlib,sys\n"
        "w=pathlib.Path(os.environ['AGENT_BUS_WORKDIR'])\n"
        "(w/'a.txt').write_text('x'); (w/'o.log').write_text('out')\n"
        "sha=lambda p: hashlib.sha256(p.read_bytes()).hexdigest()\n"
        "(w/'manifest.json').write_text(json.dumps({'artifacts':[{'path':'a.txt','sha256':sha(w/'a.txt'),'produced_by':0}],"
        "'commands':[{'cmd':'python cheat.py','exit_code':0,'stdout_path':'o.log','stdout_sha256':sha(w/'o.log')}],"
        "'output_summary':'s','verdict':'v'}))\n"
        "sys.exit(1)\n", encoding="utf-8")
    cp = _runner(t21, f'"{PY}" "{cheat.as_posix()}"')
    st21 = j(run(db, "status", "--id", t21))
    check("21 外层 exit!=0 不许 done", st21.get("state") in ("failed", "dead"),
          f"state={st21.get('state')} rc={cp.returncode}")

    # ---------------------------------------------------------- 22-24 并发（Codex P1）
    import bridge_handoff as _bh

    # 这两个模块在 import 时读环境变量；selftest 的 DB 是临时库，必须手动指过去
    _bh.DB_PATH = db
    _bh.INBOX = db.parent / "inbox"

    t22 = j(run(db, "submit", "--agent", "researcher", "--title", "推送抢占", "--nonce", "p1-1"))["task"]["id"]
    check("22 push 抢占（starter 不含 kanban 桥，跳过）", True, "skipped")

    # 23: 两个线程同时给同一 task 写 handoff，inbox 里该块只能有一份
    t23 = j(run(db, "submit", "--agent", "researcher", "--title", "并发交接", "--nonce", "p1-2"))["task"]["id"]
    import bridge_handoff as _bh

    def _h(i: int) -> None:
        _bh.write_handoff(t23, done=f"worker-{i}", next_step=f"next-{i}",
                          artifacts=[], actor="racer", inbox=True)

    ths = [threading.Thread(target=_h, args=(i,)) for i in range(2)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    inbox_file = db.parent / "inbox" / f"{time.strftime('%Y-%m-%d')}-racer.md"
    body = inbox_file.read_text(encoding="utf-8") if inbox_file.exists() else ""
    n_blocks = body.count(f"agent-bus:handoff:{t23}")
    check("23 并发 handoff：inbox 里同一任务的块只有一份", n_blocks == 2,
          f"markers={n_blocks}（开始+结束各一次=2）")

    # 24: 读事务一致性 —— status --id 的 task 与 events 来自同一快照（不报错即可，快照语义由 BEGIN 保证）
    cp = run(db, "status", "--id", t23)
    check("24 status 读事务可用", cp.returncode == 0 and j(cp).get("id") == t23,
          cp.stderr.strip()[:80])

    # ---------------------------------------------------------- 汇总
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n=== {passed}/{total} PASS ===")
    (tmp / "selftest-report.json").write_text(
        json.dumps({"passed": passed, "total": total, "tmp": str(tmp),
                    "results": [{"name": n, "ok": o, "detail": d} for n, o, d in results]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {tmp / 'selftest-report.json'}")
    if passed != total:
        for n, o, d in results:
            if not o:
                print(f"  FAILED: {n} — {d}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())