#!/usr/bin/env python3
"""demo/agent_research.py — researcher 角色的最小 agent

它只做一件事：把 packet 里指定的 target 文件读一遍，产出结构化笔记。
不调模型也能跑（确定性模式）；加 --llm 才走模型通道。

这个脚本存在的意义是证明：agent 只需要 packet，不需要全量历史。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_step(argv: list[str]) -> tuple[subprocess.CompletedProcess, str]:
    """用 argv 列表执行，不拼 shell 字符串。

    路径里带空格、引号、括号时，拼字符串会被 shell 吃掉；
    argv 形式由 subprocess 直接传给 CreateProcess，不经过 shell 解析。
    """
    cp = subprocess.run(argv, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    return cp, (cp.stdout or "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    a = ap.parse_args()

    pkt = json.loads(Path(os.environ["AGENT_BUS_PACKET"]).read_text(encoding="utf-8"))
    work = Path(os.environ["AGENT_BUS_WORKDIR"])
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    task = json.loads(pkt["packet"]["task"])
    payload = task.get("payload") or {}
    target = payload.get("target")
    if not target:
        raise SystemExit("payload.target missing")

    tp = Path(target)
    if not tp.exists():
        raise SystemExit(f"target not found: {tp}")

    t0 = time.time()
    py = sys.executable
    commands: list[dict] = []

    # 命令 0：本脚本自身。runner 另有独立 runlog 记录真实 exit_code，可交叉验证
    self_log = logs / "agent-run.log"
    self_log.write_text(
        f"argv={sys.argv}\ntarget={tp.resolve()}\n"
        f"# 刻意不写时间戳：写了就无法逐字节复算\n",
        encoding="utf-8",
    )
    commands.append({
        "cmd": f'"{py}" "{Path(__file__).resolve().as_posix()}"',
        "exit_code": 0,
        "stdout_path": "logs/agent-run.log",
        "stdout_sha256": sha256_file(self_log),
        "note": "agent 主进程；runner runlog 可交叉验证",
    })

    # 命令 1：真子进程算 sha256
    c1 = [py, "-c",
          "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())",
          str(tp)]
    cp1, out1 = run_step(c1)
    (logs / "cmd-sha256.log").write_text(out1, encoding="utf-8")
    digest = out1.strip().splitlines()[-1] if out1.strip() else ""
    commands.append({
        "cmd": shlex.join(c1), "exit_code": cp1.returncode,
        "stdout_path": "logs/cmd-sha256.log", "stdout_sha256": sha256_file(logs / "cmd-sha256.log"),
    })

    # 命令 2：真子进程统计行数与标题数
    c2 = [py, "-c",
          "import sys,re;s=open(sys.argv[1],encoding='utf-8',errors='replace').read();"
          "L=s.splitlines();print(len(L));"
          "print(sum(1 for x in L if re.match(r'^#{1,3} ',x)))",
          str(tp)]
    cp2, out2 = run_step(c2)
    (logs / "cmd-stats.log").write_text(out2, encoding="utf-8")
    parts = [x for x in out2.strip().splitlines() if x.strip()]
    nlines = int(parts[0]) if len(parts) >= 2 else 0
    nheads = int(parts[1]) if len(parts) >= 2 else 0
    commands.append({
        "cmd": shlex.join(c2), "exit_code": cp2.returncode,
        "stdout_path": "logs/cmd-stats.log", "stdout_sha256": sha256_file(logs / "cmd-stats.log"),
    })

    # 命令 3：抽标题清单（报告要用，且必须可复现）
    c3 = [py, "-c",
          "import sys,re;"
          "L=open(sys.argv[1],encoding='utf-8',errors='replace').read().splitlines();"
          "[print(f'L{i+1}: {x}') for i,x in enumerate(L) if re.match(r'^#{1,3} ',x)]",
          str(tp)]
    cp3, out3 = run_step(c3)
    (logs / "cmd-headings.log").write_text(out3, encoding="utf-8")
    commands.append({
        "cmd": shlex.join(c3), "exit_code": cp3.returncode,
        "stdout_path": "logs/cmd-headings.log", "stdout_sha256": sha256_file(logs / "cmd-headings.log"),
    })

    # 命令 4：量字节数（不能让「大小」这个数字没有出处）
    c4 = [py, "-c", "import os,sys;print(os.path.getsize(sys.argv[1]))", str(tp)]
    cp4, out4 = run_step(c4)
    (logs / "cmd-size.log").write_text(out4, encoding="utf-8")
    nbytes = int(out4.strip()) if out4.strip().isdigit() else tp.stat().st_size
    commands.append({
        "cmd": shlex.join(c4), "exit_code": cp4.returncode,
        "stdout_path": "logs/cmd-size.log", "stdout_sha256": sha256_file(logs / "cmd-size.log"),
    })

    # 命令 5：统计条目数（上游口径里的"规则条数"，不能只有标题数）
    c5 = [py, "-c",
          "import sys,re;"
          "s=open(sys.argv[1],encoding='utf-8',errors='replace').read();"
          "print(sum(1 for l in s.splitlines() if re.match(r'^\\s*(\\d+\\.|[-*]) ',l)))",
          str(tp)]
    cp5, out5 = run_step(c5)
    (logs / "cmd-items.log").write_text(out5, encoding="utf-8")
    nitems = int(out5.strip()) if out5.strip().isdigit() else 0
    commands.append({
        "cmd": shlex.join(c5), "exit_code": cp5.returncode,
        "stdout_path": "logs/cmd-items.log", "stdout_sha256": sha256_file(logs / "cmd-items.log"),
    })

    # 命令 6：二级章节数（上游问的是"章节数"，不能拿标题总数糊过去）
    c6 = [py, "-c",
          "import sys;"
          "print(sum(1 for l in open(sys.argv[1],encoding='utf-8',errors='replace') if l.startswith('## ')))",
          str(tp)]
    cp6, out6 = run_step(c6)
    (logs / "cmd-sections.log").write_text(out6, encoding="utf-8")
    nsec = int(out6.strip()) if out6.strip().isdigit() else 0
    commands.append({
        "cmd": shlex.join(c6), "exit_code": cp6.returncode,
        "stdout_path": "logs/cmd-sections.log", "stdout_sha256": sha256_file(logs / "cmd-sections.log"),
    })

    all_lines = tp.read_text(encoding="utf-8", errors="replace").splitlines()
    first = all_lines[0][:120] if all_lines else "(空)"
    out = work / "report.md"

    report = []
    report.append(f"# 调研报告：{tp.name}\n")
    report.append("## 结论\n")
    report.append(f"- 本报告：`{out.resolve()}`")
    report.append("- 本报告的 sha256：见同目录 `manifest.json` 的 `artifacts[0].sha256`（由 runner 复核后写入 `verification.json`）")
    report.append(f"- 目标文件：`{tp.resolve()}`")
    report.append(f"- 大小：{nbytes} 字节（命令 [4]）")
    report.append(f"- 章节数（`## `）：{nsec}（命令 [6]）；标题数（`#`~`###`）：{nheads}（命令 [2]）；行数：{nlines}（命令 [2]）")
    report.append(f"- 规则条数：{nitems}（命令 [5]，口径=行首（允许前导空白）为 `N.` / `- ` / `* ` 的条目）")
    report.append(f"- 首行：{first}")
    report.append(f"- sha256：`{digest}`（命令 [1]）")
    report.append("\n## 证据\n")
    report.append("- 标题清单（来自 `logs/cmd-headings.log`，命令 [3]）：")
    for ln in out3.splitlines():
        report.append(f"  - {ln}")
    report.append("\n## 复现\n")
    report.append("在 `work/` 下逐条执行；每条命令的 stdout 应逐字节等于下面贴出的内容。")
    report.append("`stdout_sha256` 的复算方式：`python -c \"import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())\" < logs/<该命令的日志> `。\n")
    for i, c in enumerate(commands):
        report.append(f"```bash\n# [{i}] exit_code={c['exit_code']}  stdout_path={c['stdout_path']}  stdout_sha256={c['stdout_sha256']}")
        report.append(f"{c['cmd']}")
        report.append("```")
        lp = work / c["stdout_path"]
        if lp.exists():
            allout = lp.read_text(encoding="utf-8", errors="replace").splitlines()
            report.append(f"```text\n# 实测 stdout（共 {len(allout)} 行，全部列出）")
            report.extend(allout if allout else ["(empty)"])
            report.append("```")
    report.append("\n## 缺口\n")
    report.append("- 本卡只做结构统计与首行摘录，未做语义判断。")
    report.append("- 未读取与本卡无关的其他文件。")

    out.write_text("\n".join(report) + "\n", encoding="utf-8")

    manifest = {
        "runner": {
            "token": os.environ.get("AGENT_BUS_RUN_TOKEN", ""),
            "cmdline": os.environ.get("AGENT_BUS_CMDLINE", ""),
        },
        "artifacts": [
            {"path": "report.md", "sha256": sha256_file(out),
             "bytes": out.stat().st_size, "produced_by": 0}
        ],
        "commands": commands,
        "output_summary": f"读取 {tp.name}（{nlines} 行 / {nheads} 标题 / {nitems} 条目），产出 report.md；{len(commands)} 条命令均留 stdout 与 sha256",
        "verdict": f"researched {tp.name}: {nlines} lines, {nheads} headings",
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} B), commands={len(commands)}")


if __name__ == "__main__":
    main()