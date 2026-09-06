# Several sessions at once

You can have as many sessions as you need, including several to the same host.
Each is a separate daemon with its own connection and its own shell, and they do
not intersect: verified that `cd /etc` in one does not affect another.

This is the intended way to do parallel work — start a process in one session
and watch it from another. An important detail about pacing: **one Bash call
costs the agent about 15 seconds**, so "look three times with pauses" as
separate calls is impossible for a task shorter than a minute — by the first
measurement everything is already over. Make the pauses with a remote `sleep`
**inside** `run`, and take a series of measurements in a single call:

```bash
$S new worker server.example.com
$S new watcher server.example.com

$S run worker 'rm -f /tmp/job.log; (for i in $(seq 1 10); do echo "tick-$i" >> /tmp/job.log; sleep 1; done) &'
$S run watcher 'wc -l < /tmp/job.log; sleep 3; wc -l < /tmp/job.log; sleep 3; wc -l < /tmp/job.log'
$S run worker 'jobs'      # meanwhile worker is not blocked
```

**A session name is a machine-wide resource.** All sessions live in one
directory, so anyone using the same name lands in the same shell. What exists
and what does not:

- `ls` and `info` show the owner (`same owner` / `other:5367636a`);
- `new` refuses a name held by a **live** session (code 2) and will not silently
  hijack someone else's shell. A dead name is reused by an ordinary `new`;
  `--force` is not needed for that;
- `kill --all` will close other people's sessions too — in a shared directory,
  kill by name.

**There is no automatic protection between parallel agents of one chat, and
there cannot be.** Verified: a subagent inherits the parent's
`CLAUDE_CODE_SESSION_ID` unchanged, its environment matches the parent's byte
for byte. So all subagents of one chat see each other as `same owner`, and `ls`
will not tell your session apart from a neighbouring agent's.

Two consequences follow, both mandatory:

1. **Name the session after the task, not `box`** — `deploy-web`, `logs-nginx`,
   `db-migrate`. In a subagent flow, hand every agent its own name explicitly.
   This is the only real protection.
2. If you need genuine isolation, set the owner yourself through
   `SSHSESS_OWNER`; it overrides everything else:

```bash
SSHSESS_OWNER=agent-a $S new a-deploy server.example.com
SSHSESS_OWNER=agent-b $S ls          # will see a-deploy as other:agent-a
```

Dead sessions stay in `ls` on purpose — their log is needed for the post-mortem.
To drop the records along with the logs: `$S prune`.

**Parallel `run`s into one session are serialised.** The remote shell executes
one thing at a time, so two simultaneous `run`s used to interleave their output —
A would receive B's wrapper inside itself. Now they queue on a flock and each
gets its own clean output. If there is no time to wait, the second exits with
code **125** saying the session is busy. `--timeout` is the shared budget for
"wait for the queue plus execute", so a call never hangs longer than requested.

`send` deliberately does **not** take the lock: `send --key C-c` has to get
through precisely when something is running in the session.

For a subagent flow: give every agent its own session name. Then they physically
do not interfere with each other rather than relying on the queue.

