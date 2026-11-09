# agent-playbook

**A task bus for multi-agent systems where "the agent said it's done" is not evidence.**

Most multi-agent frameworks solve *who speaks next*. They ship nothing for the failure modes that actually bite: duplicate runs, orphaned tasks, unverifiable output, and actions taken before approval.

This package is a working answer to those four, plus the field notes from getting them wrong first.

---

## What's inside

| Path | What |
|---|---|
| `starter/` | Minimal task bus. 30 tests, Python stdlib only, zero dependencies |
| `docs/避雷清单.md` | 21 real failures with root cause and fix (Chinese) |
| `docs/轻量模型守则.md` | 12 structural constraints so weak models still deliver (Chinese) |
| `docs/对比.md` | Honest comparison against CrewAI / OMA / Aegis, with current star counts |
| `docs/验收清单.md` | Pre-delivery checklist |
| `examples/` | How one evidence gate went from fake to real, over four review rounds |
| `skills/` | Three methodology docs you can drop into an agent |

---

## 60-second demo

```bash
cd starter
python bus.py init
bash ../demo.sh
```

You will see, in order:

1. Submitting the same task twice creates **one** row (nonce idempotency)
2. Running a task that was never claimed is **refused** by the executor
3. Claim → start → execute in an isolated directory
4. The verification record: outer exit code, real command line, stdout hash
5. Modifying an artifact by one byte makes `done` **fail**
6. An outward-facing task stays `blocked_on_human` until a human decides
7. The context budget: how many bytes the agent actually received

All real runs. No mocks.

---

## The four things it actually guarantees

| Guarantee | Mechanism |
|---|---|
| The same task never runs twice | `client_nonce UNIQUE` + atomic `claim` under `BEGIN IMMEDIATE` |
| A crashed worker doesn't orphan work | Lease with expiry, `reap`, `attempts` cap → `dead` |
| "Done" means there's evidence | Artifact sha256 + per-command `exit_code` / `stdout_sha256` / `produced_by` |
| Side effects wait for approval | Four categories: outbound / spend / delete / permission. Unapproved tasks cannot be claimed |
| Terminal states stay terminal | `block` refuses `done`/`failed`/`dead`; `approve` only handles pending approvals |
| Non-zero exit is never success | Outer process `exit_code != 0` fails before manifest validation |

---

## The one idea

> **Push uncertainty into structure, not into the model's judgement.**

Three consequences:

- A task without an acceptance criterion will be faked — by strong models too.
- Weak models differ most on *open-ended planning*, least on *constrained execution*.
- State lives on disk; context is a cache.

---

## What it does not do

- No planner. You still decide what to submit.
- No model routing or capability profiling.
- No group chat. Coordination peaks at 2–3 roles; past ~5 they pollute each other.
- No multi-machine. Single SQLite file.
- No web UI. CLI and files only.

**It also cannot prove which command produced an artifact.** The manifest is written by the agent. What the runner guarantees is that the outer process exited 0 and its own run log is out of the agent's reach. That boundary is documented, not hidden.

---

## Status

- `python selftest.py` → **30/30 PASS**
- Reviewed adversarially by Codex 0.163: 7 P0 findings, all fixed, each with a test
- No keys, no private paths, no customer data

---

## License

MIT (add your own file if you publish).