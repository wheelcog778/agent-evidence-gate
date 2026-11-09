#!/usr/bin/env python3
"""runner.py — 最小沙箱执行器

一个 task => 一个隔离工作目录 runs/<id>/，里面：
  packet.json       agent 能看到的全部上下文（预算内）
  INSTRUCTIONS.md   给 agent 的 prompt
  work/             agent 的工作目录（产物写这里）
  manifest.json     agent 必须写的交付清单
  stdout.txt / stderr.txt

校验规则（缺一不可，否则任务不能 done）：
  - manifest.json 存在且是合法 JSON
  - artifacts[].path 存在，sha256 与 manifest 声明一致
  - commands 非空（证明真跑过）
  - output_summary 非空
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

import bus
from context import build_packet, render_prompt

ROOT = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("AGENT_BUS_RUNS", ROOT / "runs"))


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR {msg}", file=sys.stderr)
    sys.exit(code)


def resolve_inside(raw: str, workdir: Path) -> Path | None:
    """把 manifest 里的路径解析到 workdir 内；越界返回 None。

    agent 可以把任意绝对路径或 ../../ 写成自己的"产物"，
    那样哈希能对上，但产物根本不是它做的。证据必须落在自己的沙箱里。
    """
    q = Path(raw)
    q = q.resolve() if q.is_absolute() else (workdir / q).resolve()
    try:
        q.relative_to(workdir.resolve())
    except ValueError:
        return None
    return q


def verify_manifest(manifest: dict, workdir: Path, expected: dict | None = None) -> tuple[bool, list[str]]:
    """证据门 v4

    规则（缺一不可）：
      1. artifacts 非空，每个 path 在 workdir 内、存在、sha256 **必填**且一致
      2. 每个 artifact 必须带 produced_by（指向 commands 的下标）
      3. commands 非空，每条是对象：{cmd, exit_code, stdout_path, stdout_sha256}
         cmd 必须是非空字符串；字符串形式的「我跑过 X」不再接受
      4. 每条命令的 stdout_path 在 workdir 内、必须是普通文件、sha256 一致
      5. 至少一条命令 exit_code == 0
      6. 外层进程退出码由 cmd_run 单独把关，不在这里判

    注意（不是缺陷，是边界）：manifest 由 agent 写，所以"这条命令真的产生这个文件"
    runner 无法从外部观测。真正不可伪造的是 runlog（runner 侧）与外层 exit_code。
    """
    problems: list[str] = []

    arts = manifest.get("artifacts")
    if not isinstance(arts, list) or not arts:
        problems.append("manifest.artifacts missing or empty")
        arts = []

    cmds = manifest.get("commands")
    if not isinstance(cmds, list) or not cmds:
        problems.append("manifest.commands missing or empty")
        cmds = []

    ok_cmd_idx: set[int] = set()
    for i, c in enumerate(cmds):
        if not isinstance(c, dict):
            problems.append(
                f"commands[{i}] is a string; commands must be objects "
                "{{cmd, exit_code, stdout_path, stdout_sha256}} — a bare claim is not evidence"
            )
            continue
        missing = [k for k in ("cmd", "exit_code", "stdout_path", "stdout_sha256") if k not in c]
        if missing:
            problems.append(f"commands[{i}] missing {missing}")
            continue
        if not isinstance(c["cmd"], str) or not c["cmd"].strip():
            problems.append(f"commands[{i}].cmd empty")
            continue
        try:
            exit_code = int(c["exit_code"])
        except (TypeError, ValueError):
            problems.append(f"commands[{i}].exit_code not an int: {c['exit_code']!r}")
            continue
        sp = resolve_inside(str(c["stdout_path"]), workdir)
        if sp is None:
            problems.append(f"commands[{i}].stdout_path escapes workdir: {c['stdout_path']}")
            continue
        if not sp.is_file():
            problems.append(f"commands[{i}].stdout_path not a regular file: {sp}")
            continue
        got = sha256(sp)
        if got != c["stdout_sha256"]:
            problems.append(f"commands[{i}].stdout_sha256 mismatch (declared={str(c['stdout_sha256'])[:12]} actual={got[:12]})")
            continue
        if exit_code == 0:
            ok_cmd_idx.add(i)

    if cmds and not ok_cmd_idx:
        problems.append("no command with exit_code == 0 (nothing actually succeeded)")

    for i, a in enumerate(arts):
        if not isinstance(a, dict) or not a.get("path"):
            problems.append(f"artifacts[{i}] has no path")
            continue
        p = resolve_inside(str(a["path"]), workdir)
        if p is None:
            problems.append(f"artifacts[{i}].path escapes workdir: {a['path']}")
            continue
        if not p.is_file():
            problems.append(f"artifact not a regular file: {p}")
            continue
        declared = a.get("sha256")
        if not isinstance(declared, str) or not declared:
            problems.append(f"artifacts[{i}] ({p.name}) has no sha256 — 必填，不能省略")
            continue
        got = sha256(p)
        if got != declared:
            problems.append(f"sha256 mismatch: {p.name} declared={declared[:12]} actual={got[:12]}")
        pb = a.get("produced_by")
        if pb is None:
            problems.append(f"artifacts[{i}] ({p.name}) has no produced_by — cannot tell which command made it")
        elif not isinstance(pb, int) or not (0 <= pb < len(cmds)):
            problems.append(f"artifacts[{i}].produced_by={pb!r} out of range")
        elif pb not in ok_cmd_idx:
            problems.append(f"artifacts[{i}].produced_by={pb} points at a failed/unverified command")

    if not (manifest.get("output_summary") or "").strip():
        problems.append("manifest.output_summary empty")
    if not (manifest.get("verdict") or "").strip():
        problems.append("manifest.verdict empty (agent produced nothing judgeable)")
    return (not problems), problems


def cmd_run(a: argparse.Namespace) -> None:
    bus.init_if_needed()
    # 前置门（Codex P0-1）：任务必须是 running、租约未过期、审批已过、未超期。
    # 不检查就启动，等于绕过了审批与状态机——副作用已经发生，后面的 done 门拦不住。
    c = bus.conn(readonly=True)
    try:
        t = c.execute("SELECT * FROM task WHERE id=?", (a.id,)).fetchone()
    finally:
        c.close()
    if not t:
        die(f"task not found: {a.id}")
    if t["state"] != "running":
        die(f"runner refuses: task state={t['state']}, must be running (claim+start first)")
    if t["lease_until"] and t["lease_until"] < time.time():
        die("runner refuses: lease expired; run `bus.py reap` and re-claim")
    if t["require_approval"] and t["approval_state"] != "approved":
        die(f"runner refuses: approval not granted (state={t['approval_state']})")
    if t["deadline"] and time.time() > t["deadline"]:
        die("runner refuses: deadline passed")

    rundir = RUNS / a.id
    work = rundir / "work"
    work.mkdir(parents=True, exist_ok=True)

    pkt = build_packet(a.id, a.budget)
    (rundir / "packet.json").write_text(json.dumps(pkt, ensure_ascii=False, indent=2), encoding="utf-8")
    (rundir / "INSTRUCTIONS.md").write_text(render_prompt(pkt), encoding="utf-8")

    env = dict(os.environ)
    env["AGENT_BUS_TASK_ID"] = a.id
    env["AGENT_BUS_PACKET"] = str((rundir / "packet.json").resolve())
    env["AGENT_BUS_WORKDIR"] = str(work.resolve())
    env["PYTHONIOENCODING"] = "utf-8"

    # 支持 {ROOT}/{WORK} 占位符：cwd 是隔离的 work/，相对路径会指错
    cmdline = (a.cmd
               .replace("{ROOT}", str(ROOT).replace("\\", "/"))
               .replace("{WORK}", str(work).replace("\\", "/")))

    # 只给一个本轮标识，不作为凭据：真正的信任来自 runner 侧的 runlog 与外层 exit_code
    env["AGENT_BUS_RUN_ID"] = secrets.token_hex(6)

    t0 = time.time()
    try:
        cp = subprocess.run(
            cmdline, shell=True, cwd=str(work), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=a.timeout,
        )
        out, err, code = cp.stdout or "", cp.stderr or "", cp.returncode
        timed_out = False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        code, timed_out = -9, True
    elapsed = round(time.time() - t0, 2)

    (rundir / "stdout.txt").write_text(out, encoding="utf-8")
    (rundir / "stderr.txt").write_text(err, encoding="utf-8")

    # runner 自己的执行记录：agent 改不到（不在 work/ 下）
    runlog = {
        "task_id": a.id,
        "cmdline": cmdline,
        "exit_code": code,
        "timed_out": timed_out,
        "elapsed_ms": int(elapsed * 1000),
        "stdout_path": str((rundir / "stdout.txt").resolve()),
        "stdout_sha256": sha256(rundir / "stdout.txt"),
        "stdout_bytes": (rundir / "stdout.txt").stat().st_size,
        "stderr_path": str((rundir / "stderr.txt").resolve()),
        "stderr_sha256": sha256(rundir / "stderr.txt"),
        "stderr_bytes": (rundir / "stderr.txt").stat().st_size,
        "context": {"used_bytes": pkt["used_bytes"], "full_history_bytes": pkt["full_history_bytes"],
                    "saved_ratio": pkt["saved_ratio"], "comparable": pkt.get("comparable", True)},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (rundir / "runlog.json").write_text(json.dumps(runlog, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "task_id": a.id, "elapsed_s": elapsed, "exit_code": code, "timed_out": timed_out,
        "context": runlog["context"],
        "run_dir": str(rundir.resolve()),
        "runlog": str((rundir / "runlog.json").resolve()),
    }

    mf = work / "manifest.json"
    verification = {"runlog": runlog, "manifest": None, "ok": False, "problems": []}
    if timed_out:
        result["ok"] = False
        result["reason"] = f"timeout after {a.timeout}s"
        verification["problems"] = [result["reason"]]
        _call("fail", a.id, reason=result["reason"])
    elif code != 0:
        # Codex P0-6：外层进程非零退出，不允许进入 manifest 门。
        # 否则 agent 写完一份自洽的清单再 exit(1)，仍会被判成功。
        result["ok"] = False
        result["reason"] = f"agent exited {code}; manifest gate skipped"
        verification["problems"] = [result["reason"]]
        _call("fail", a.id, reason=result["reason"])
    elif not mf.exists():
        result["ok"] = False
        result["reason"] = "manifest.json not written by agent"
        verification["problems"] = [result["reason"]]
        _call("fail", a.id, reason=result["reason"])
    else:
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception as e:
            manifest = {}
            result["ok"] = False
            result["reason"] = f"manifest.json invalid: {e}"
            verification["problems"] = [result["reason"]]
            _call("fail", a.id, reason=result["reason"])
        else:
            try:
                ok, problems = verify_manifest(manifest, work)
            except Exception as e:
                # Codex P1：畸形 manifest 不许把 gate 打崩，只能把任务判失败
                ok, problems = False, [f"gate raised {type(e).__name__}: {e}"]
            result["ok"] = ok
            result["problems"] = problems
            result["manifest"] = manifest
            verification.update({"manifest": manifest, "ok": ok, "problems": problems})
            if ok:
                # 证据 = runner 的核验记录，不是 agent 的自述
                (rundir / "verification.json").write_text(
                    json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
                ev = _call("evidence", a.id, path=str((rundir / "verification.json").resolve()))
                if ev != 0:
                    verification["problems"] = [f"bus evidence rejected (rc={ev}); task not marked done"]
                    result["ok"] = False
                else:
                    dn = _call("done", a.id, verdict=(manifest.get("verdict") or "")[:500])
                    if dn != 0:
                        verification["problems"] = [f"bus done rejected (rc={dn})"]
                        result["ok"] = False
            else:
                _call("fail", a.id, reason="manifest gate: " + "; ".join(problems))

    (rundir / "verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
    (rundir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _call(cmd: str, tid: str, **kw) -> int:
    args = [sys.executable, str(ROOT / "bus.py"), cmd, "--id", tid]
    for k, v in kw.items():
        if v is None:
            continue
        args += [f"--{k.replace('_', '-')}", str(v)]
    try:
        cp = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=60)
    except Exception as e:
        print(f"[error] bus {cmd} could not run: {type(e).__name__}: {e}", file=sys.stderr)
        return 99
    if cp.returncode != 0:
        print(f"[warn] bus {cmd} failed: {cp.stderr.strip()}", file=sys.stderr)
    return cp.returncode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--cmd", required=True, help="要执行的命令（在 work/ 下跑）")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--budget", type=int, default=8192)
    a = ap.parse_args()
    cmd_run(a)


if __name__ == "__main__":
    main()