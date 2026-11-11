# Relay bridge: putting a GUI-only worker behind the same evidence gate

`bridge_relay.py` lets a task run on a worker that **cannot execute commands** — a desktop
chat app, a browser tab, a human-in-the-loop model — while keeping the two properties the
bus exists for:

1. the artifact is tied to *this* dispatch, not to something left over from last week;
2. the worker's own claim of "done" is not the thing that closes the task.

It does this without modifying `bus.py`. The relay takes the worker's place in the state
machine and the worker is reduced to what it can actually do: write files.

---

## The problem, stated precisely

A GUI worker has no shell, no callback, no token. So it cannot run `bus.py claim`,
`bus.py evidence` or `bus.py done`. If you relay the task by hand, three things go wrong:

| Attempt | What happens |
|---|---|
| The worker is told to claim it itself | `claim` succeeds as `agent=worker`; the relay's later `claim` returns `null` (`state=claimed`) and the dispatch never happens |
| The relay claims first, then the worker tries | `claim` returns `null`; if it keeps trying it looks like a stuck task |
| The worker claims, the relay tries to submit | `evidence`/`done` are accepted only from the owner; the relay gets an owner mismatch and the task rots in `claimed` until `reap` |

Measured on a real bus (temporary database, isolated run):

```
submit                     -> state=queued
worker claim               -> state=claimed, owner=worker
relay dispatch (claim)     -> claim_failed: state=claimed
relay submit while worker owns -> owner mismatch
relay claims first         -> owner=relay
worker claim               -> null (state=claimed)
```

The fourth row is the reason this bridge exists: ownership is single-writer, so **exactly
one party may speak to the bus**, and it has to be the party that also keeps the snapshot.

## What the bridge does

```
bus.py submit --agent gui-worker --title "..." --payload '{...}'
        │
        ▼
bridge_relay.py dispatch --id t_xxx
        │   claim --agent gui-worker --owner relay   (the worker's identity, the relay's owner)
        │   start                                     (claimed -> running: evidence needs it)
        │   write relay_out/t_xxx/BRIEF.md            (self-contained: paths, output dir, prohibitions)
        │   write relay_inbox/t_xxx.md                (first line = the message a human pastes)
        │   snapshot relay_out/t_xxx/                 (path -> mtime,size at dispatch time)
        ▼
   human pastes one line into the GUI window
        │
        ▼
worker writes files into relay_out/t_xxx/
        │
        ▼
bridge_relay.py collect --id t_xxx
            diff snapshot  -> only files that did not exist, or changed, since dispatch
            mtime check    -> a file older than the dispatch is `stale_rejected`, not evidence
            copy           -> relay_out/t_xxx/evidence/, sha256(original) == sha256(copy)
            bus.py evidence --path <primary copy>
            bus.py done
            event: relayed  {relayed_by, worker, worker_ran_bus: false, sources[...]}
```

## Quick start

```bash
python bus.py submit --agent gui-worker --title "Review the spec" \
  --payload '{"must_read":["docs/relay-bridge.md"],"expected_artifact":"review.md"}'

python bridge_relay.py dispatch --id t_xxx
# paste the first line of relay_inbox/t_xxx.md into the worker's window

python bridge_relay.py collect --id t_xxx      # evidence + done
python bridge_relay.py status  --id t_xxx      # what is new, what was ignored
```

`dispatch` prints the exact line to paste (`wake_first_line`); you never compose it by hand.
The brief is generated, not templated by the human, so the worker always sees the same
contract: read only these paths, write here, do not call the bus.

## Guarantees

- **The task cannot close without a file.** `collect` refuses with `no_fresh_artifact`
  when nothing new exists; the bus would refuse `done` anyway.
- **Old artifacts cannot be passed off as new.** Two independent conditions: the path is
  absent from the dispatch snapshot, and its mtime is strictly later than the dispatch.
- **Evidence is durable.** Files are copied into `evidence/` before submission and the
  copy's hash is compared to the original; deleting the worker's session directory later
  does not destroy the evidence.
- **Provenance is in the event log.** A `relayed` event records `relayed_by`, the worker
  name, `worker_ran_bus: false`, and every source path with its hash. The bus's own
  `evidence` + `done` events remain the primary record.
- **The relay identity is not the worker identity.** `claim --owner` separates "which
  agent the task belongs to" from "which process actually holds the lease". Anyone reading
  `status` can see both.

## Limits (read these before trusting it)

- **`mtime` is not a signature.** A worker that deliberately backdates a file, or writes
  one before the dispatch and waits, is out of scope. The gate makes forgery visible
  (`stale_rejected`, snapshot diffs), not impossible.
- **The bus stores one evidence path.** The bridge copies every fresh artifact, submits the
  primary one (the declared `expected_artifact`, else the largest), and records the rest in
  `manifest.json` and in the `relayed` event. If you need several files gated individually,
  split the task.
- **There is no heartbeat for a GUI worker.** `claim --lease` is fixed at dispatch; the lease
  is not extended. Pass a lease longer than the job, or `reap` will requeue a task that is
  still being worked on. `dispatch --lease 7200` is the default for that reason.
- **External files are not accepted silently.** Files written outside the output directory
  are listed; if nothing landed in the output directory, `collect` refuses
  (`external_artifact_unconfirmed`) until you pass `--accept-external`. If the output
  directory did produce an artifact, external files are recorded as `external_ignored` and
  not attached — a worker that keeps its own notes should not stall the run.
- **The bridge is not a sandbox.** It cannot stop a worker from editing a read-only path;
  it only guarantees such a file will not be collected as evidence. Use a copy/snapshot of
  the source tree and a hash check if you need to prove nothing was touched.

## Self-test

```bash
python bridge_relay.py selftest     # 16 checks, temporary DB, no network
```

It exercises the failure paths on purpose: a second dispatch, a missing artifact, a
pre-dispatch file, an external-only submission, and the record that the worker never
called the bus. It prints `=== 16/16 PASS ===` and the temporary directory it used.

## When to use this instead of `runner.py`

`runner.py` runs a command in an isolated work directory and verifies its manifest. Use it
whenever the worker can be executed. Use the relay bridge only when the worker is a window
you type into — the bridge then becomes the only part that can lie about provenance, which
is why it writes the snapshot before the human is even asked to paste the message.