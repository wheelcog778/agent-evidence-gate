#!/usr/bin/env python3
"""pack.py — 打交付包

只收录"能自己跑起来"的文件，不收录 bus.db / runs/ / 临时日志。
包内附 SHA256SUMS.txt 与交付说明.md。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = Path.home() / "交付"
VERSION = "v0.2"

INCLUDE = [
    "bus.py", "context.py", "runner.py", "bridge_handoff.py", "bridge_kanban.py",
    "e2e.py", "selftest.py", "pack.py",
    "demo/agent_research.py", "demo/agent_verify.py", "demo/agent_llm.py",
    "profiles/researcher.md", "profiles/verifier.md",
    "RULES.md", "README.md", "REPORT-2026-11-08.md",
    "codex-review.md", "codex-brief.txt", "e2e-final.log",
]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main() -> int:
    date = datetime.now().strftime("%Y%m%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = OUT_DIR / f"agent-bus-{VERSION}-{date}.zip"

    # 先跑一遍测试，把报告落在项目内
    print("[1/4] selftest")
    cp = subprocess.run([sys.executable, "selftest.py"], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", cwd=str(ROOT))
    tail = [ln for ln in cp.stdout.splitlines() if "PASS ===" in ln]
    print("   ", tail[-1] if tail else cp.stdout[-300:])
    if cp.returncode != 0:
        print("selftest 未通过，拒绝打包", file=sys.stderr)
        return 1

    report_src = None
    for ln in cp.stdout.splitlines():
        if ln.startswith("report: "):
            report_src = Path(ln[len("report: "):].strip())
    if report_src and report_src.exists():
        shutil.copy2(report_src, ROOT / "selftest-report.json")

    files = [f for f in INCLUDE if (ROOT / f).exists()]
    missing = [f for f in INCLUDE if not (ROOT / f).exists()]
    if missing:
        print(f"   缺文件（将跳过）：{missing}")

    print(f"[2/4] 算哈希 {len(files)} 个文件")
    sums = []
    for f in files:
        p = ROOT / f
        sums.append(f"{sha256(p)}  {f}")
    (ROOT / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")

    print("[3/4] 写交付说明")
    nlines = {}
    for f in files:
        if f.endswith(".py"):
            nlines[f] = sum(1 for _ in (ROOT / f).open(encoding="utf-8", errors="replace"))
    doc = f"""# agent-bus {VERSION} 交付说明

生成时间：{datetime.now():%Y-%m-%d %H:%M}
包内文件：{len(files)} 个（另有 SHA256SUMS.txt 与本说明）

## 这是什么

多 agent 任务总线的最小可运行版：SQLite 状态机 + 租约 + 证据门 + 审批门 + 预算式上下文包。
Python 3.14 标准库，Windows 11 + git-bash，单机。

## 五分钟验证

```bash
cd agent-bus
python bus.py init
python selftest.py        # 期望 22/22 PASS
python e2e.py             # 真链路，会调一次模型（约 2 分钟）
```

## 包内结构

| 文件 | 作用 |
|---|---|
| `bus.py` ({nlines.get('bus.py','?')} 行) | 状态机、租约、证据门、审批门、CLI |
| `context.py` ({nlines.get('context.py','?')} 行) | 四槽预算上下文包 |
| `runner.py` ({nlines.get('runner.py','?')} 行) | 沙箱执行 + manifest 校验 |
| `bridge_handoff.py` ({nlines.get('bridge_handoff.py','?')} 行) | AGENTS.md §5 五列交接 |
| `bridge_kanban.py` ({nlines.get('bridge_kanban.py','?')} 行) | Hermes crew 对接 |
| `selftest.py` ({nlines.get('selftest.py','?')} 行) | 22 项断言 |
| `e2e.py` ({nlines.get('e2e.py','?')} 行) | 三跳真链路 |
| `demo/` | 三个角色：确定性 / 独立核验 / 真模型 |
| `README.md` | 设计、命令表、功能清单、已知缺口 |
| `REPORT-2026-11-08.md` | 六轮迭代记录与实测数据 |
| `codex-review.md` | 外部（Codex）对抗性审查结论 |

## 不包含

- `bus.db`：运行期生成，里面是测试数据
- `runs/`：执行现场，体积大且含临时产物
- 任何密钥：全文无 API key；`demo/agent_llm.py` 从环境变量读取

## 状态

- `selftest.py` 最近一次：见 `selftest-report.json` 的 `passed/total`
- 未接推送（靠轮询 claim）、单机 SQLite、审批只有 CLI —— 详见 README「已知缺口」

## 边界

只读 `AGENTS.md` 与 `交接/`，未改任何既有规则文件、未碰任何产品本体、未发版到任何产品。
"""
    (ROOT / "交付说明.md").write_text(doc, encoding="utf-8")
    files = files + ["SHA256SUMS.txt", "交付说明.md"]
    if (ROOT / "selftest-report.json").exists():
        files.append("selftest-report.json")

    print(f"[4/4] 打包 -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(ROOT / f, f"agent-bus/{f}")

    print(f"   {zip_path.stat().st_size:,} B, {len(files)} 件")
    print(f"   sha256 {sha256(zip_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())