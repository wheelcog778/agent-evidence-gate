#!/usr/bin/env python3
"""demo/agent_llm.py — 接真模型的 agent（qwen 通道，OpenAI 兼容）

它证明的是：模型侧也只需要 packet，不需要全量历史。
用法：
  runner.py --id <t> --cmd "python demo/agent_llm.py"

环境：
  AGENT_BUS_PACKET / AGENT_BUS_WORKDIR 由 runner 注入
  HERMES_CUSTOM_QWEN_API_KEY 或 QWEN_API_KEY
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("BUS_LLM_BASE", "https://api.openai.com/v1")
MODEL = os.environ.get("BUS_LLM_MODEL", "gpt-4o-mini")
KEY = os.environ.get("HERMES_CUSTOM_QWEN_API_KEY") or os.environ.get("QWEN_API_KEY") or ""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def call(messages: list[dict], max_tokens: int = 4000, retries: int = 3) -> tuple[str, dict]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": max_tokens}).encode("utf-8")
            req = urllib.request.Request(
                BASE + "/chat/completions", data=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                r = json.load(resp)
            msg = r["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            return text, r.get("usage", {})
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise SystemExit(f"model call failed after {retries} attempts: {type(last).__name__}: {last}")


def main() -> None:
    pkt = json.loads(Path(os.environ["AGENT_BUS_PACKET"]).read_text(encoding="utf-8"))
    work = Path(os.environ["AGENT_BUS_WORKDIR"])
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    task = json.loads(pkt["packet"]["task"])
    payload = task.get("payload") or {}
    question = payload.get("question") or task.get("title")
    target = payload.get("target")

    if not KEY:
        raise SystemExit("no API key in env")

    material = ""
    if target and Path(target).exists():
        raw = Path(target).read_text(encoding="utf-8", errors="replace")
        material = raw[:6000]

    upstream_verdict = payload.get("upstream_verdict") or ""

    sys_prompt = pkt["packet"].get("brief") or f"你是 {pkt['agent']}。"
    rules = pkt["packet"].get("rules") or ""
    upstream = pkt["packet"].get("upstream") or ""

    user = f"""问题：{question}

上游交来的（只作参考，不要复述）：
{upstream or '（无）'}

上游 verifier 的实测结论（它有权执行命令并读文件；这些是它跑出来的，不是你跑出来的）：
```
{upstream_verdict or '（本次无实测记录）'}
```

材料摘录：
```
{material or '（无）'}
```

你的角色边界：
- 你只做**文本一致性审查**：报告里写的东西自洽吗、口径写清了吗、数字有没有出处、命令能不能照抄。
- **实测由上游 verifier 完成**，不在你的职责内。你不需要、也没有能力重算 sha256 或重跑命令。
- 因此你的结论格式必须是：「文本一致性：PASS/FAIL」+「实测：见上游」。不要因为"我没实测"就把整份报告判 FAIL——那是职责错配。

要求：
1. 先给结论（不超过 60 字）。
2. 再给依据，每条必须有出处（文件路径或摘录里的原文片段）。
3. 最后给缺口：你没看到、不能确定的部分。
4. **你没有任何工具**：不能执行命令、不能读文件、不能上网。只能依据上面贴出的文本判断。
   不要输出任何工具调用、XML 标签、`<tool_calls>`、伪代码。
不要写客套话，不要复述问题。
"""

    text, usage = call([
        {"role": "system", "content": sys_prompt + ("\n\n硬规则：\n" + rules if rules else "")},
        {"role": "user", "content": user},
    ])

    # 模型幻觉自己有工具时，产出是废的；不许把它当交付
    import re as _re
    hallucinated = bool(_re.search(r"<tool_calls?>|<invoke|</tool_", text))

    out = work / "answer.md"
    out.write_text(f"# {task.get('title')}\n\n{text}\n", encoding="utf-8")

    run_log = logs / "agent-run.log"
    run_log.write_text(
        f"argv={sys.argv}\nmodel={MODEL}\nquestion={question}\n"
        f"prompt_tokens={usage.get('prompt_tokens')}\ntotal_tokens={usage.get('total_tokens')}\n"
        f"reasoning_tokens={(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')}\n"
        f"text_tokens={(usage.get('completion_tokens_details') or {}).get('text_tokens')}\n",
        encoding="utf-8",
    )

    manifest = {
        "runner": {
            "token": os.environ.get("AGENT_BUS_RUN_TOKEN", ""),
            "cmdline": os.environ.get("AGENT_BUS_CMDLINE", ""),
        },
        "artifacts": [
            {"path": "answer.md", "sha256": sha256(out), "bytes": out.stat().st_size, "produced_by": 0}
        ],
        "commands": [
            {
                "cmd": f"python demo/agent_llm.py (model={MODEL})",
                "exit_code": 0,
                "stdout_path": "logs/agent-run.log",
                "stdout_sha256": sha256(run_log),
                "note": "模型调用；runner runlog 记录真实退出码与耗时",
            }
        ],
        "output_summary": f"模型回答 {len(text)} 字；prompt_tokens={usage.get('prompt_tokens')}; text_tokens={(usage.get('completion_tokens_details') or {}).get('text_tokens')}"
                          + ("；检测到幻觉工具调用，产出作废" if hallucinated else ""),
        "verdict": ("" if hallucinated else (text.splitlines()[0][:120] if text.strip() else "")),
        "hallucinated_tool_call": hallucinated,
        "usage": usage,
    }
    (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} B), tokens={usage.get('total_tokens')}")


if __name__ == "__main__":
    main()