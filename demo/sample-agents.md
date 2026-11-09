# Handoff Protocol (sample document)

This is a deliberately small sample used by `e2e.py`. It mimics a team handoff
protocol so the demo agents have something real to read, count and hash.

## 0. Who you are

You are one of several builders on this machine. Read the current state, claim one
task, finish it verifiably, write the facts back to disk.

## 1. Read before work

1. The delivery index: current outputs and open risks only.
2. The task board: claimable rows.
3. At most three documents related to the claimed task.

No recursive greps over the home directory.

## 2. Claiming a task

- Skip rows marked `doing` with a heartbeat under two hours old.
- Skip `blocked` rows: those wait for a human, not for you.
- Claim with an atomic lock: `mkdir locks/<task-id>`; success means yours.
- Update the board immediately: status, owner, heartbeat.

## 3. Hard boundaries

1. Product code under release is read-only.
2. Original deliverables are read-only.
3. Never write a real key into an artifact, a log or a commit.
4. Research needs a pre-registration before any numbers are produced.
5. A negative result is a normal result; do not move the goalposts.

## 4. What counts as done

Three things on disk:

1. Artifact: absolute path plus size or hash.
2. Verification: the command you actually ran and its output.
3. Caliber: every number recomputable or traced to a file and a line.

## 5. Write-back

Append one row to `inbox/<date>-<agent>.md`:

| time | what upstream asked | what I did | artifact paths | next first step |

## 6. Environment facts

- Windows, git-bash shell. Native programs need forward-slash paths.
- Long jobs write to disk first, report second.