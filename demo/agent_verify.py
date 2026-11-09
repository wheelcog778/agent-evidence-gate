#!/usr/bin/env python3
"""demo/agent_verify.py — verifier 角色的最小 agent

只吃两样东西：packet（含 upstream_files）+ 上游产物本身。
独立重算 sha256、独立检查章节；不看上游的思考过程。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REQUIRED_SECTIONS = ("## 结论", "## 证据", "## 缺口")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-fail", action="store_true")
    a = ap.parse_args()

    pkt = json.loads(Path(os.environ["AGENT_BUS_PACKET"]).read_text(encoding="utf-8"))
    work = Path(os.environ["AGENT_BUS_WORKDIR"])
    logs = work / "logs"
    logs.mkdir(exist_ok=True)

    files = pkt.get("upstream_files") or []
    files = [f for f in files if f.lower().endswith(".md")]
    if not files:
        raise SystemExit("upstream_files empty: nothing to verify")

    target = Path(files[0])
    if not target.exists():
        raise SystemExit(f"upstream artifact missing: {target}")

    text = target.read_text(encoding="utf-8", errors="replace")
    actual_sha = sha256_file(target)

    # 留痕：agent 自己的执行记录 + 真实重算命令的 stdout
    (logs / "agent-run.log").write_text(
        f"argv={sys.argv}\nverifying={target.resolve()}\nsha256={actual_sha}\n", encoding="utf-8")
    py = sys.executable
    hash_argv = [py, "-c",
                 "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())",
                 str(target)]
    cp = subprocess.run(hash_argv, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    (logs / "cmd-sha256.log").write_text(cp.stdout or "", encoding="utf-8")
    if cp.returncode != 0:
        raise SystemExit(f"hash command failed rc={cp.returncode}: {(cp.stderr or '')[:200]}")
    hash_cmd = shlex.join(hash_argv)
    checks = []

    # 1) 文件存在与可读
    checks.append(("上游产物存在", True, str(target.resolve())))

    # 2) 章节完整性
    missing = [s for s in REQUIRED_SECTIONS if s not in text]
    checks.append(("必需章节齐全", not missing, f"missing={missing}" if missing else "3/3 present"))

    # 3) 上游 manifest 声明的 sha256 重算
    mf = target.parent / "manifest.json"
    if mf.exists():
        m = json.loads(mf.read_text(encoding="utf-8"))

        def _resolve(p: str) -> Path:
            # 上游 manifest 里的 path 可能是相对的：相对于上游产物目录，不是本进程 cwd
            q = Path(p)
            return q.resolve() if q.is_absolute() else (target.parent / q).resolve()

        declared = next((x.get("sha256") for x in m.get("artifacts", [])
                         if _resolve(str(x.get("path", ""))) == target.resolve()), None)
        actual = actual_sha
        checks.append(("sha256 与上游声明一致", declared == actual,
                       f"declared={str(declared)[:16]}… actual={actual[:16]}…"))
    else:
        checks.append(("上游 manifest 存在", False, "manifest.json not found next to artifact"))

    # 4) 上游不能声称没跑过的事：report 里必须有 sha256 行（本 demo 的契约）
    checks.append(("报告含可复算指纹", "sha256" in text.lower(), "sha256 line present" if "sha256" in text.lower() else "absent"))

    # 5) 上游必须给出可重跑的命令与输出哈希（证据门 v2）
    has_repro = "## 复现" in text
    checks.append(("报告含 ## 复现 节", has_repro, "present" if has_repro else "missing — 无命令即不可复现"))

    # 故意制造一次 FAIL，用来验证总线不会被"假通过"骗过
    if a.expect_fail:
        checks.append(("人工注入的负面用例", False, "injected failure for gate test"))

    ok = all(c[1] for c in checks)
    verdict = "PASS" if ok else "FAIL"

    md = [f"结论：{verdict}", "", "## 核对项", ""]
    for name, passed, detail in checks:
        md.append(f"- {'PASS' if passed else 'FAIL'} · {name} — {detail}")
    md += ["", f"## 被测对象", "", f"- `{target.resolve()}`", f"- {len(text)} 字符", ""]
    out = work / "verdict.md"
    out.write_text("\n".join(md), encoding="utf-8")

    manifest = {
        "runner": {
            "token": os.environ.get("AGENT_BUS_RUN_TOKEN", ""),
            "cmdline": os.environ.get("AGENT_BUS_CMDLINE", ""),
        },
        "artifacts": [
            {"path": "verdict.md", "sha256": sha256_file(out), "bytes": out.stat().st_size, "produced_by": 0}
        ],
        "commands": [
            {
                "cmd": f'"{py}" "{Path(__file__).resolve().as_posix()}"',
                "exit_code": 0,
                "stdout_path": "logs/agent-run.log",
                "stdout_sha256": sha256_file(logs / "agent-run.log"),
                "note": "独立重算，不采信上游自述；runner runlog 可交叉验证",
            },
            {
                "cmd": hash_cmd,
                "exit_code": cp.returncode,
                "stdout_path": "logs/cmd-sha256.log",
                "stdout_sha256": sha256_file(logs / "cmd-sha256.log"),
            },
        ],
        "output_summary": f"独立核验 {target.name}：{verdict}（{sum(1 for c in checks if c[1])}/{len(checks)} 项通过）",
        "verdict": verdict,
    }
    (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} verdict={verdict}")


if __name__ == "__main__":
    main()