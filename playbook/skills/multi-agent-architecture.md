---
name: multi-agent-architecture
description: "Use when designing or sizing an agent-team runtime."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [multi-agent, architecture, agent-runtime, sandbox, approval, orchestration]
    related_skills: [kanban-multi-agent-team, agent-output-audit, hermes-agent]
---

# Multi-Agent Architecture

Designing an agent team, an agent runtime, or a "digital colleague" system — and deciding
whether to build one at all. This is the design pass that comes BEFORE `kanban-multi-agent-team`
(which is how to operate a Hermes board).

## When to Use

- "Design me a multi-agent team like <product>", "how should this be architected", "which
  framework should I use".
- Before building an execution/orchestration layer from scratch.
- When asked whether an existing crew is enough.

## 0. Decompose the target before designing anything

Every agent-team product is four layers. Vendors advertise layer 3; the product's actual
quality lives in layers 1 and 4.

| Layer | What it is | What breaks without it |
|---|---|---|
| **Execution environment** | Persistent workspace per account, logical isolation per agent, shared filesystem | Task runs, but nothing accumulates; every run re-authenticates and re-downloads |
| **Identity & messaging** | Agent registry; agent and room as one entity type; message delivery as a first-class API | Agents can't hand off, can't see each other, can't be addressed |
| **Orchestration** | Deterministic turn scheduler: nonce dedupe, deadline, winding-down, recursion depth | Duplicate work, ping-pong loops, runaway fan-out, token burn |
| **Control** | Scheduler (cron/event), approval gate, credential broker | It cannot be trusted with anything that spends money, sends, deletes, or logs in |

## 1. Design rules that decide whether it works

1. **Room = Agent.** Make a group a row in the same table as an agent, reusing transcript,
   unread, notification and permission systems, plus one turn-orchestration layer. A separate
   "group chat" subsystem doubles the state you must keep consistent.
2. **The orchestrator is a deterministic program, not a supervisor LLM.** It decides which
   members get a turn, which increments they see, the deadline, and when to wind down. Each
   member model returns SENT or PASS. A supervisor model costs more, is unpredictable, and
   floods the channel.
3. **LLM decides what and whether; the program decides who, when, where, and whether it's safe.**
   Keep system behaviors (billing, cleanup, approval) out of the prompt.
4. **Secrets never enter the transcript.** Values travel a side channel; the transcript records
   only "provided". Approval cards are typed (action / blast radius / value / rollback) and
   **timeout = deny**.
5. **State lives on disk or in the control plane with cursors, never only in an agent's context.**
   Context is a cache; the log is the truth.
6. **Events are idempotent.** Every inbound task carries a client-generated nonce with a unique
   constraint. "Accepted" and "completed" are two different promises — only promise accepted
   synchronously.

## 2. Minimal build: three components, not a framework

```text
task(id, parent_id, agent, state, payload, client_nonce UNIQUE,
     created_at, deadline, evidence_path, verdict)
state: queued -> claimed -> running -> blocked_on_human -> done | failed | dead | cancelled
```

- `client_nonce UNIQUE` = idempotency (a retried dispatch never double-runs).
- `parent_id` = causal chain, which is also the recursion-depth limit.
- `deadline` = timeout rejects instead of hanging forever.
- `evidence_path` = no artifact, no `done`. This is the machine version of "verified, not claimed".
  Presence alone is not evidence: the gate must also require a still-matching hash, a non-empty
  verdict, and — for anything meant to be reproducible — the command that produced the artifact,
  its exit code and its output hash. Free-text `commands: ["..."]` proves nothing.

**Sandbox Runner**: one isolated working dir + one agent process per task; the process must
write `manifest.json` (paths, sha256, commands run, output summary). One VM per task is
over-engineering — production systems share one box per account and isolate logically.

**Approval Gate**: intercept exactly four categories — outbound (messages, publishing),
spending, delete/overwrite, permission change. Everything else runs unattended.

**Do not build a group chat.** Coordination peaks at 2-3 roles; past ~5, members pollute each
other's context and the value drops below a plain pipeline. Pipelines plus an independent
reviewer beat a room for research, backtests and audits.

**Before calling it done:** hand the code to a second agent for a read-only adversarial review with
a fixed P0/P1/P2 output shape (skill `codex`), verify each finding in the source yourself, then
package and re-run the suite **from inside the extracted copy** (skill `agent-output-audit` §4.8).
Self-review reliably misses the guard you documented but never implemented, and a suite you wrote
to prove your own gates will pass on the holes they were meant to catch.

## 3. Build-vs-buy gate (ask this before agreeing to build)

- Existing crew already runs a full week unattended → **do not build**; you'd get a second
  system that splits attention.
- A human still hand-dispatches and hand-judges which output is credible → build the three
  components above, not another crew.
- Need a product others will use → take an execution-plane base (OpenHands / OMA) and write
  only the control plane and domain knowledge. Never use CrewAI / MetaGPT / AutoGen as the
  runtime backbone: they solve "who speaks next" and ship none of idempotency, approval,
  credentials, or sandbox lifecycle.

On this machine the equivalents already exist — map before proposing new parts: `delegate_task`
≈ agent registry, the handoff task board + `mkdir` locks ≈ orchestrator, Windows scheduled jobs
≈ scheduler, `AGENTS.md` §4 ≈ acceptance gate. Name what is genuinely missing rather than
re-proposing what runs. A worked implementation of §2 also lives at `this repository`
(state machine, runner, evidence gate, context packet, handoff and kanban bridges, self-test) —
read it before writing a second one, and extend it rather than forking a variant.

## 4. Reference point: Grok Bot (xAI + Cursor, 2026-08)

Worth studying because it is the most complete public instance of the four layers.

- **Marketing vs implementation**: "each bot has its own computer" is really one persistent VM
  **per account**, per-agent screens/VNC sessions, shared `/workspace`. Official docs state bots
  are not a security boundary. Verify a product's claims against a reverse-engineering analysis
  before designing from them.
- **Seven invariants**, of which only **one** is multi-agent (room = agent). Four are client
  engineering (local snapshot renders first, optimistic send + idempotent reconcile,
  intent-driven wake, event-driven reconnect probe) and explain the "fast and never drops"
  praise. Budget accordingly: copying the *experience* is client work, not agent work.
- Round protocol worth stealing: `nonce` (dedupe), `peers[]` (profiles only, never the other
  agent's transcript), `new_messages[]` (incremental), `is_winding_down`, `deadline_ms`,
  `root_parent_id` (depth limit), outcome `SENT | PASS | SKIPPED | TIMEOUT | CANCELLED | ERROR`.
- Adoption order: core loop (execution + optimistic send + cursors + reconnect + one agent +
  cards + secret side channel) → multi-agent (room isomorphism, deterministic turns, memory
  sharding) → delegation (credentials, secure forms, channels).

## 5. Pitfalls

- **Stars are not capability.** The most-starred frameworks (MetaGPT, AutoGen, CrewAI) lack
  persistent execution, idempotency and approvals. Judge by which of the four layers the repo
  actually implements (see `references/framework-landscape.md`).
- **An evidence gate that only checks "file exists + hash matches" is defeated by a one-line
  claim.** Require each command to be an object `{cmd, exit_code, stdout_path, stdout_sha256}`,
  each artifact to carry `produced_by` (index of the command that made it), and register the
  *runner's* verification record as evidence rather than the agent's own manifest. Commands in a
  reproducibility list must be pure: one embedded timestamp (`started=...`) or a relative path
  makes the hash unrecomputable and the whole claim unverifiable. Resolve every declared path
  against the task's own work dir and require containment — an absolute path or `../` otherwise
  lets an agent nominate someone else's existing file as its artifact, hash and all.
  **A field the gate treats as optional is the bypass**: `if artifact.get("sha256")` makes the hash
  decorative. Every field the gate reasons about is mandatory, and a record missing one is rejected
  outright rather than checked partially. Start strict — loosening a gate later is easy, tightening
  it after agents have learned to clear it is not.
- **Models hallucinate having tools.** A text-only reviewer will emit `<tool_calls>` XML and
  claim it ran shell commands it cannot run. State "you have no tools" in the prompt and treat
  any tool-call markup in the output as a void deliverable, not as an answer.
- **Verification needs two passes, and the second must be fed the first's result.** One agent
  with file/subprocess access does the real re-computation; a second, tool-less agent checks
  internal consistency of the written report. Pass the measured verdict into the reviewer's
  payload and write the boundary into its prompt ("you judge text consistency only; measurement is
  upstream's job"). A tool-less reviewer held to "no unverified claim may pass" will otherwise
  refuse the whole report — correctly — and return a permanent FAIL that reads like a content
  defect when it is a role mismatch.
- **A receipt the producer can read is not a receipt.** Injecting a per-run token at launch and
  requiring the manifest to echo it blocks *replay* of an old manifest — nothing more, because the
  agent reads the same environment variable and writes it into a fresh lie. If you build one, name
  it "replay protection" and never let it stand in for forgery defense. What is genuinely outside
  the agent's control is the runner's own `runlog` plus **the outer process exit code**: an agent
  that writes a perfectly self-consistent manifest and then exits non-zero must be failed without
  entering the manifest gate. Deleting a pseudo-receipt is better than shipping it under a name
  that implies more.
- **Gates belong before the side effect, not only at the report.** A runner that checks approval,
  state and lease when marking a task `done` has already launched the command — the money moved,
  the message went out. Re-read state, lease freshness, approval and deadline inside the same
  transaction that starts execution and refuse to launch. A gate checked on one exit and not the
  other is the standard hole.
- **An outbound side effect needs a reservation, not a sent-flag.** Two workers that both read
  "rows not yet delivered" and then both call the external system deliver twice, and a crash after
  the call but before the flag loses the delivery. Reserve inside `BEGIN IMMEDIATE` with
  `INSERT … SELECT … WHERE NOT EXISTS` so exactly one owner exists, call the outside world, then
  settle: success flips the reservation to done, failure **deletes** it so the next tick retries.
  Never write the done-flag before the call returns.
- **Two sinks without a transaction drift apart; and WAL does not give you a snapshot per
  connection.** A DB row plus a file written as separate steps can end up one-sided, and readers
  can observe a half-written file. Write the file inside the transaction and publish it with a
  temp-file + `os.replace` (old or new content, never partial). Any view that answers one question
  with several SELECTs needs an explicit `BEGIN` on the read connection — under WAL each statement
  takes its own snapshot, so a status payload can pair a new state with a stale event list.
- **Terminal states come back through the door you left open.** A `block` that accepts any state,
  plus an `approve` that only checks "is blocked", resurrects `done`/`dead`. Likewise a
  `fail --retry` path that skips the approval check re-enqueues an unapproved task, and an
  attempts-exhausted retry resets the fail budget. Keep one transition table, make every
  state-writing command consult it, and treat any requeue as a fresh admission that re-runs the
  approval gate. Draw the diagram from the table, never the table from the diagram.
- **Don't recommend a mature-sounding clone that doesn't exist.** New products spawn thin
  peripheral repos (single-digit stars) plus a reverse-engineering archive; the analysis is
  usually worth more than the code. Say plainly that no mature clone exists.
- **Failure mode is silent, not loud.** Scheduled agents report "nothing found" indistinguishably
  from "never ran". Every routine must report what it scanned and how many, not just findings.
- **Scheduling inside the sandbox dies on image upgrade.** Keep cron in the control plane and
  call into the box; a crontab baked into the image is lost on rebuild.
- **Routing by display name misdelivers.** Bind channels/targets by immutable ID; two agents can
  legitimately share a name.
- **Reasoning models spend the entire output budget thinking.** Completion tokens can be 100%
  reasoning with `text_tokens=0`, so the call returns 200 with empty content. Budget generously and
  assert non-empty text; an empty artifact plus a permissive gate is a silent failure, not an error.
- **Resolve paths before you chdir into the sandbox.** A runner that executes the agent with `cwd`
  set to the isolated work dir breaks every relative path in the command; support explicit
  placeholders for tool roots, or the failure reads as an agent bug rather than a runner bug.
- **A negative test can pass for the wrong reason.** An uncaught exception also exits non-zero, and
  CPython echoes the offending source line in the traceback — so `assert "must be running" in
  stderr` passes on the traceback of the very bug the assertion was meant to detect. Assert the
  deliberate error marker (`ERROR <prefix>:`) *and* assert `"Traceback" not in stderr`. Every gate
  needs at least one assertion that could only pass if the gate worked; a suite of
  "exit code != 0" assertions certifies nothing.

## References

- `references/framework-landscape.md` — open-source options by layer, with what each one really
  covers and how to re-verify counts.
- `references/task-bus-build.md` — implementation companion to §2: schema, claim SQL, gate
  semantics, packet budgets and metric discipline, runner contract, selftest assertion list, and
  the traps that cost a cycle.
