# sshsess — test cases

A smoke set for `scripts/sshsess.py`. It runs **only against the local sshd**
from `tests/local-sshd.sh`. No remote hosts: production machines are off limits,
and the tests are deliberately built so that none are needed.

## Setup

```bash
SKILL=$(dirname "$(find ~/.claude ~/.config .claude -path '*ssh-session/scripts/sshsess.py' 2>/dev/null | head -1)")/..
bash "$SKILL/tests/local-sshd.sh" start
S="$SKILL/scripts/sshsess.py"
C=/tmp/sshsess-test/ssh_config
```

The local login shell is fish, so working sessions are opened like this (case F3
checks the opposite — that without `--shell` you get a clear refusal):

```bash
$S new NAME local-test --shell 'bash -i' -- -F $C
```

Rules of execution:

- **Prefix session names with your group's letter** (`a-`, `b-`, …). Other
  executors work in parallel, and a session name is a machine-wide resource.
- **Do not call `kill --all` or `prune`** — you will wipe out other people's
  sessions. Clean up only your own, by name.
- **Do not call `local-sshd.sh stop`.** An executor cannot know it is the last
  one; the stand is stopped by whoever handed out the assignments.
- The calling shell is zsh (`$0` = `/usr/bin/zsh`; `Shell: /bin/fish` in the
  environment is the user's login shell, not the calling shell). zsh does **not**
  word-split: `-- $ARGS` is substituted as a single word and ssh answers
  `Bad port '…'`. Write `-- -F $C`, do not collect arguments into a variable.
- The `VAR=value $S …` construct (needed in E6/E7) works in zsh; if you are
  executing through fish, wrap it in `bash -c`.
- Do not use `pkill -f` / `pgrep -f` with a pattern that occurs in your own
  command line: it will match itself and kill your call. Here the "remote" host
  is localhost, so that is especially easy to arrange.
- For cases where the locale affects the text (`ls /nope`, `jobs`), check the
  **exit code and the fact that a message is present**, not the English wording:
  on this machine the messages are in Russian.
- Every case is a separate checkable fact. Record the **actual** output and exit
  code, do not restate the expectation. A discrepancy is a finding, not a reason
  to "fix" the test.

---

## A. Lifecycle

| ID | What is checked | How | Expected |
|---|---|---|---|
| A1 | sshd is bound to loopback only | `ss -tln \| grep 2222` | the **Local Address** column shows `127.0.0.1:2222`. `0.0.0.0:*` is always in the line — that is the Peer Address column |
| A2 | a session opens | `$S new a-1 local-test --shell 'bash -i' -- -F $C` | `session 'a-1' ready`, code 0 |
| A3 | `info` on a live session | `$S info a-1` | target, `live`, owner, pty and the log path are present |
| A4 | `ls` shows the session | `$S ls` | a line `a-1 live … same owner` |
| A5 | state survives between Bash calls | `run a-1 'cd /tmp'` in one call, `run a-1 'pwd'` in **another** | `/tmp` |
| A6 | `kill` closes it | `$S kill a-1; $S ls` | `killed 'a-1'`, then `a-1 dead` |
| A7 | `info` on a dead session is useful | `$S info a-1` | code 3, but the log path and `ssh_exit` are printed |
| A8 | a dead name is reused without `--force` | `$S new a-1 local-test --shell 'bash -i' -- -F $C` | code 0 |
| A9 | a live name is not handed over | `$S new a-1 …` again | code 2, the text `is already running`, the session intact |
| A10 | `--force` evicts a live one | first `run a-1 'cd /etc; pwd'` → `/etc`; then `new … --force`; then `run a-1 'pwd'` | code 0, `pwd` = the home directory. Without the first step the case passes vacuously: the session already sits in `$HOME` |
| A11 | an invalid name is rejected | `$S new 'a/1' local-test -- -F $C` | code 2, `invalid session name` |
| A12 | `kill` on a nonexistent one does not lie | `$S kill a-nope` | code **3**, `no such session 'a-nope'`. A false `killed 'a-nope'` with code 0 is a FAIL |
| A13 | `kill` on an already dead one | `$S kill a-1` twice | the second time: `'a-1' was already dead`, no traceback |

## B. `run` correctness

| ID | What is checked | How | Expected |
|---|---|---|---|
| B1 | the output is clean | `$S run b-1 'echo hi'` | exactly `hi` — no prompt, no echo, no `__SS…` markers |
| B2 | exit codes | `false` → 1, `ls /nope` → 2, `sh -c 'exit 7'` → 7, `nosuchcmd` → 127 | all match |
| B3 | cwd and env persist | `run 'cd /tmp && export Z=42'`, then `run 'pwd; echo Z=$Z'` | `/tmp` and `Z=42` |
| B4 | stderr arrives too | `$S run b-1 'ls /nope'` | the error text is in the call's stdout |
| B5 | large output is not broken | `$S run b-1 'seq 1 20000' \| md5sum` — compute the md5 **locally** over the received stream and compare with `seq 1 20000 \| md5sum` | the md5s match. Computing the md5 remotely is not allowed: only 34 bytes would come out, and the case would pass even if the output were truncated |
| B6 | a long line is not wrapped | `$S run b-1 'printf "%3000s" "" \| tr " " x' \| tr -d '\n' \| wc -c` — measure the length **locally** | `3000`. A remote `wc -c` cannot detect the wrap at the pty's 200th column, which is the whole point of the case |
| B7 | unicode | `run 'echo "Привет — ёжик 日本語 🚀"'` | the string is intact |
| B8 | the command's flags are not eaten | `$S run b-1 ls -la /etc/hostname` | the output of `ls -la`, not an argparse error |
| B9 | a trailing `&` | `run 'sleep 30 &'`, then `run 'jobs'` | code 0, the job is visible in `jobs` |
| B10 | compound constructs | `run 'for i in 1 2 3; do echo n$i; done'` | three lines |
| B11 | quoting is preserved | `run 'echo "a  b"; echo '"'"'c$d'"'"''` | the double space is intact, `$d` is not expanded |
| B12 | empty output | `run 'true'` | **0 bytes** (not a newline), code 0 |
| B13 | timeout | `$S run --timeout 2 b-1 'echo pre; sleep 30'` | code 124, `pre` in the output, a message about it being partial, no `__SS` in the output |
| B14 | recovery after a timeout | **immediately after B13**: `$S interrupt b-1`, then `run b-1 'echo ok'` | `ok`, code 0. Outside the B13 pairing the case degenerates into a plain `echo` |
| B15 | argument order | `$S run b-1 --timeout 5 'echo x'` | code **127**, `bash: --timeout: command not found` — the whole tail became the remote command and `x` is not printed. Control: `run --timeout 5 b-1 'echo x'` → `x`, code 0 |

## C. Protection against closing the session

| ID | What is checked | How | Expected |
|---|---|---|---|
| C1 | `exit` is rejected | `$S run c-1 'exit 42'` | code 2, `refusing to run this`, the session **alive** |
| C2 | `logout` is rejected | `$S run c-1 'logout'` | code 2 |
| C3 | `exit` in a chain | `$S run c-1 'ls /nope || exit 1'` | code 2 (it would kill the session too) |
| C4 | a subshell is allowed | `$S run c-1 '(exit 42)'` | code **42**, the session alive |
| C5 | `sh -c` is allowed | `$S run c-1 "sh -c 'exit 7'"` | code **7**, the session alive |
| C6 | a word inside a string is not a command | `$S run c-1 'echo "the word exit inside"'` | code 0, the line is printed |
| C7 | the explicit opt-in works | `$S run --allow-exit c-1 'exit 9'` | code **0** and `session 'c-1' closed by request` — no emergency dump. Afterwards `run c-1` → code 3, `ls` → `dead` |

## D. `send` / `read` / `wait`

| ID | What is checked | How | Expected |
|---|---|---|---|
| D1 | `send --wait` shows only what is new | seed the log with a marker line beforehand, then `$S send d-1 'echo scoped' --wait 1` | `scoped` is visible (plus the command's echo and the prompt — that is normal), but **not** the seeded marker and not the whole log from the start |
| D2 | entering a REPL | take an offset with `read d-1 --offset` (it goes to **stderr**), then `send d-1 'python3 -q'`, then `wait d-1 '>>>' --since OFFSET` | `>>>` found, code 0. Without `--since` there is a race: `wait` only sees bytes after its own start, and if the prompt arrived earlier the case hangs for the whole timeout |
| D3 | a multi-line block in a REPL | `send d-1 --paste 'def fib(n):\n    …'` **as a single argument with real newlines**, then `send d-1 '' --wait 1`, then `send d-1 'fib(30)' --wait 2` | `832040`. Without `--paste` on Python ≥3.13 you get `IndentationError`: PyREPL adds indentation to every continuation line itself |
| D4 | leaving a REPL | `send d-1 --key C-d`, then `run d-1 'echo back'` | `back`, code 0 |
| D5 | `read --tail` | `$S read d-1 --tail 5` | no more than 5 lines |
| D6 | internal markers are hidden | `$S read d-1 --tail 30` and `--tail 30 --no-filter` | the first has no `__SS…`, the second does |
| D7 | `--raw` gives the raw stream | `$S read d-1 --raw \| cat -v \| grep -c '\^\['` | greater than zero. `\| head` will not do: the start of the log has only `^M`. Extra check: the output **without** `--raw` must contain no `^[` at all |
| D8 | `--since` | `$S read d-1 --offset 2>/tmp/off >/dev/null`, take the number from `/tmp/off`, then `read --since $OFF --all` | only the output after the offset. `--offset` is printed to **stderr**; without `--all`, `--tail 100` is applied on top of `--since`. Do not write `$(… 2>&1 >/dev/null \| …)`: in zsh with MULTIOS, stdout lands in the same substitution and digits from the log end up in the offset |
| D9 | `wait` waits for **new** output | `send d-1 'for i in 1 2 3 4 5; do echo step-$i; sleep 1; done'`, then `time $S wait d-1 'step-5'` | found, took **≈4 s** (`step-5` is printed after the fourth `sleep`). An instant answer is a FAIL: the echo was caught |
| D10 | `wait` on timeout | `$S wait d-1 'NEVER' --timeout 3` | code 124 |
| D11 | `wait --from-start` | search for a line that was already in the log | found instantly |

## E. Parallelism and isolation

| ID | What is checked | How | Expected |
|---|---|---|---|
| E1 | sessions are independent | `cd /tmp` in `e-1`, `cd /etc` in `e-2`, then `pwd` in both | `/tmp` and `/etc` |
| E2 | real parallelism | in `e-1` a background job writes a line every second for 12 s; from `e-2`, three measurements in **one** `run` with a remote `sleep 3` between them | the line count grows at least three times (e.g. 1→5→9), and `e-1` answers `run` meanwhile. Important: "remote" here = localhost and the filesystem is shared, so the file growing is not by itself proof of remoteness — the proof is `run` in the busy `e-1` |
| E3 | a race into one session is serialised | two `run`s into `e-1` at once (`&` in the local shell), output into different files; measure the total time | each file is **byte-identical** to its expected output, 0 foreign lines, 0 occurrences of `__SS` and `{ printf`. Both codes 0. The time ≈ the sum of the durations (parallel execution would have given half) — that is the proof of serialisation |
| E4 | busyness is visible | while `run e-1 'sleep 5'` is going, `$S run --timeout 1 e-1 'echo x'` | code 125, `is busy`, and the answer comes **after ≈1 s** rather than instantly: `--timeout` is the shared budget for waiting on the lock plus executing |
| E5 | an interrupt unblocks `run` | `run --timeout 60 e-1 'echo pre; sleep 45'` in the background, `$S interrupt e-1` after 2 s | `run` returns **immediately** with code **130**, the session alive, the lock released (the next `run` goes through instantly). Separately: `send --key C-c` interrupts the remote command, but `run` then sits out its timeout and returns 124 — that is documented behaviour, not the same thing |
| E6 | owners differ | `SSHSESS_OWNER=x $S new e-3 …`, then `SSHSESS_OWNER=y $S ls` | `e-3` is marked `other:x` |
| E7 | someone else's name is not hijacked | `SSHSESS_OWNER=y $S new e-3 …` | code 2, the other owner is mentioned. A known hole: `--force` bypasses the owner check — record that as a fact, not as a FAIL |

## F. Errors and diagnostics

| ID | What is checked | How | Expected |
|---|---|---|---|
| F1 | a nonexistent session | `$S run f-nope 'echo x'` (a name that was **never** created — no session directory) | code 3, `is not running`, **no traceback**. Check `send`/`wait`/`info` on the same name while you are at it |
| F2 | a bad ssh argument | `$S new f-1 local-test --timeout 8 -- -F $C --bogus-flag` | code 3, the ssh launch line and its output are shown |
| F3 | a non-POSIX login shell | `$S new f-2 local-test --timeout 12 -- -F $C` (**without** `--shell`) | code 4, a `--shell 'bash -i'` hint with a ready-made command |
| F4 | a command that reads stdin | `$S run --timeout 6 f-3 'read x'` → code 124; then **necessarily** `$S interrupt f-3`; then `run f-3 'read x < /dev/null; echo rc=$?'` | without the `interrupt`, the hanging `read` eats the first line of the next command's wrapper and it fails too — that is expected behaviour, not a bug |
| F5a | a drop during `run` | `run f-5 'sleep 60'` in the background, then `kill <ssh_pid>` (the pid from `info`) | code 3 and a log tail in the message |
| F5b | a drop before `run` | kill ssh, wait, then `run` | code 3 `no live socket` — there can be no tail here, the daemon has already removed the socket; look at the tail through `info` |
| F6 | a nonexistent host | `$S new f-4 no-such-host.invalid --timeout 8` | code 3, the dump shows the line `ssh: Could not resolve hostname …`. An empty dump is a FAIL (single-line ssh errors arrive as `\r\r\n` and used to be lost) |
| F6b | connection refused | `$S new f-6 127.0.0.1 --timeout 8 -- -p 2223` (a closed port) | `Connection refused` in the dump |
| F7 | error dumps are short | look at the message of any failure | a tail of ≤ 12 lines (the whole block is ~15 with the header and separators), 0 occurrences of `__SS…`. The criterion applies to error messages; `run`'s partial output on timeout is checked separately in B13 |

## H. Prompts: keeping the work from stalling

Fixtures (create them before the group; no real passwords are used):
`fake-sudo.sh` asks for a password with echo off and accepts `s3cr3t-test-pw`,
`fake-apt.sh` prints `Do you want to continue? [Y/n] `, and `fake-press.sh`
prints `Press ENTER to continue... `. In `~/.config/sshsess/secrets.json`
(mode 0600): `{"secrets": {"sudo": "s3cr3t-test-pw"}}`.

| ID | What is checked | How | Expected |
|---|---|---|---|
| H1 | muting is applied | `new h-1 …` then `run h-1 'echo $PAGER $EDITOR $DEBIAN_FRONTEND'` | the message `(non-interactive env applied)`, the values `cat false noninteractive` |
| H2 | the pager sticks without muting | `new h-2 … --no-harden`, then `run --timeout 6 h-2 'cd <a git repo> && git log'` | code 124, the pager prompt visible in the output. Then `interrupt` |
| H3 | with muting the pager is no obstacle | `run --timeout 10 h-1 'cd <a git repo> && git log \| tail -2'` and `run h-1 'systemctl status sshd 2>&1 \| tail -2'` | code 0 for both |
| H4 | the password is substituted from the file | `run --timeout 10 h-1 fake-sudo.sh` | code 0, `AUTH_OK`, and `auto-answered sudo-password -> <secret:sudo>` on stderr |
| H5 | the password does not leak into the log | `read h-1 --all --no-filter \| grep -c s3cr3t` and `read h-1 --raw \| grep -ac s3cr3t` | **0** and **0** |
| H6 | confirmations are not answered without `--yes` | `run --timeout 6 h-1 fake-apt.sh` | code 124 (the prompt was left unanswered). Then `interrupt` |
| H7 | `--yes` answers a confirmation **once** | `run --timeout 10 --yes h-1 fake-apt.sh`, count the `auto-answered` lines | code 0, exactly **1** line (not 2: alternatives within one rule must not fire twice on the same line) |
| H8 | "Press ENTER" | `run --timeout 10 h-1 fake-press.sh` | code 0, `continued` |
| H9 | three prompts in one command | `run --timeout 20 --yes h-1 'fake-sudo.sh; fake-apt.sh; fake-press.sh'` | code 0, exactly 3 `auto-answered` lines |
| H10 | no false positives on output | `run --timeout 10 --yes h-1 'echo "Press ENTER to continue"; echo "Do you want to continue? [Y/n]"; echo done'` | code 0 and **not a single** `auto-answered` line: this is output, nobody is waiting |
| H11 | secret file permissions are checked | `chmod 644` on secrets.json, then any `run` | code 2, the text `is readable by others`. Restore with `chmod 600` |
| H12 | a missing secret | rules with `"secret":"nonexistent"`, then `run` on fake-sudo.sh | the line `PROMPT NOT ANSWERED -- … secret 'nonexistent' is not configured` (separate from `auto-answered`, so the answer count is not spoiled), then code 124 |
| H13 | `expect` on an already stuck session | `send h-1 fake-apt.sh`, wait, then `expect h-1 --dry-run --once --yes` | `would answer yes-no-confirm -> y`, nothing sent |
| H14 | `expect` unblocks | in the same place `expect h-1 --once --yes`, then `run h-1 'echo ok'` | `answered …`, then `ok` with code 0 |
| H15 | a single character as a key | `send h-1 --key q` | code 0, not `unknown key` |
| H16 | a TUI menu driven by keys | `send h-1 'whiptail --menu … 2>/tmp/choice.txt'`, wait, `send h-1 --key Down --key Enter`, then `run h-1 'cat /tmp/choice.txt'` | the second menu entry |
| H17 | `--list-rules` | `$S expect --list-rules` | **all 6** rules, code 0, without touching a session; the ones gated behind `--yes` are marked `[needs --yes]` |
| H19 | the `less -M` pager is caught | a `--no-harden` session, `run --timeout 12 'less -M /etc/services'` | code 0, one `auto-answered pager -> <key:q>` line (the pattern has to consume the tail of the line with the percentage) |
| H20 | a plain `less` is honestly not caught | in the same place `run --timeout 8 'less /etc/services'` | code 124 — the file name in inverse video is indistinguishable from output. The way out: `send --key q` |
| H18 | `--no-auto` disables it | `run --timeout 6 --no-auto h-1 fake-sudo.sh` | code 124, not a single `auto-answered` |

## I. Recovery after a stall or a drop

| ID | What is checked | How | Expected |
|---|---|---|---|
| I1 | a timeout marks the session busy | `run --timeout 4 i-1 'read x'` | code 124 |
| I2 | the next `run` refuses instead of spoiling the output | immediately after I1: `run i-1 'echo hi'` | code **126**, the text `still has a command running` plus the reason. No `syntax error` and no garbled output |
| I3 | `ls` and `info` show the busy state | `ls`, `info i-1` | `ls` shows the state `busy`; `info` has a `busy` line with the reason and a hint |
| I4 | `interrupt` clears the mark | `interrupt i-1`, then `run i-1 'echo ok'` | `interrupted … shell is back at its prompt`, then `ok` with code 0 |
| I4b | the mark clears itself once the shell is free | reproduce I1, then `send i-1 'fed in'`, then `run i-1 'echo ok'` | code 0 **without** `interrupt`: `run` probes the shell and clears the false mark |
| I4c | `interrupt` does not lie about a full-screen program | a `--no-harden` session, `run --timeout 6 … 'less /etc/services'` (124), then `interrupt` | code **126**, the text `still not reading commands`, a hint to send `--key q`. Then `send --key q` + `run` → clean |
| I5 | `--force` bypasses the check | reproduce I1, then `run --force --timeout 8 i-1 'echo x'` | code **124** after the full timeout, the output being a raw transcript. `--timeout` is mandatory: the default is 120 s of silence |
| I5b | unfinished input does not break `run` | `send i-1 --key q`, then `run i-1 'echo ok'`; repeat with `send i-1 'garbage' --no-enter` | `ok` with code 0 both times (the wrapper starts with Ctrl-U) |
| I5c | the mark does not survive a recreate | reproduce I1, then `kill i-1`, then `new i-1 …` | the line `ready … (non-interactive env applied)` and code 0; `run i-1 'echo $PAGER'` → `cat` |
| I6 | `truncate` clears the log without dropping the link | `run i-1 'seq 1 3000'`, `ls` (size), `truncate i-1`, `ls`, `run i-1 'echo after'` | the size became 0 and the command afterwards works |
| I7 | `truncate` refuses during a command | `run i-1 'sleep 5'` in the background, then `truncate i-1` | code 125, text about a command being executed |
| I8 | `reconnect` after a drop | note the cwd/a variable, `kill <ssh_pid>` (the pid from `info`), wait for `dead`, then `reconnect i-1` | code 0, the ssh arguments and `--shell` substituted automatically, muting applied again, a warning about lost state printed |
| I9 | `reconnect` does not lie about state | after I8: `run i-1 'pwd; echo [$MARK]'` | the home directory and an empty variable |
| I10 | `reconnect` with no meta | `reconnect i-no-such` (in Latin letters: a Cyrillic name is cut off by the validator with code 2 earlier) | code 3, `no recorded parameters` |

## J. Refusing to type into something that is not a prompt

The free evidence is checked (the bracketed-paste state in the log), so all the
cases run without timeouts and without waiting.

| ID | What is checked | How | Expected |
|---|---|---|---|
| J1 | an ordinary `run` is unaffected | `run j-1 'echo ok'` | `ok`, code 0 |
| J2 | a TUI started through `send` | `send j-1 'top'`, wait 2 s, then `run j-1 'echo x'` | code **126**, `not at a prompt`, advice to send the exit key. Nothing was typed into `top` |
| J3 | leaving with the native key restores operation | `send j-1 --key q`, then `run j-1 'echo ok'` | `ok`, code 0 |
| J4 | a full-screen program is named precisely | `send j-1 'whiptail --msgbox hi 8 30'`, wait, `run j-1 'echo x'` | code 126, the text `full-screen program … alternate screen`. Then `send --key Enter` and `run` → code 0 |
| J5 | `less` is caught too | `send j-1 'less /etc/services'`, wait, `run j-1 'echo x'` | code 126. Then `send --key q` and `run` → code 0 |
| J6 | a long command through `send` | `send j-1 'sleep 8'`, then immediately `run j-1 'echo x'` | code 126, `something is still running` |
| J7 | parallel `run`s are **not** broken by the check | two `run`s into `j-1` at once with `--timeout 40`, output into different files | both code 0, each with its own output. Code 126 here is a regression: the check must sit inside the lock |
| J8 | a REPL reads as a prompt (a known boundary) | `send j-1 'python3 -q'`, wait, `run j-1 'echo x'` | `run` does NOT refuse (a REPL turns bracketed paste on itself) — that is a documented limitation, not a FAIL. Leave with `--key C-d` |

## G. Portability

| ID | What is checked | How | Expected |
|---|---|---|---|
| G1a | argparse accepts `-- SSH_ARGS` on 3.8 | `uv run --no-project --python 3.8 python $S new g-1 local-test --shell 'bash -i' -- -F $C` | code 0. Before 3.12 argparse did not match `nargs="*"` after `--` if there was a flag before the `--` — that is exactly why the script parses `--` by hand |
| G1b | the 3.8 runtime completes the cycle | the same way: `run g-1 'echo ok; pwd'`, `info`, `kill` | everything goes through, code 0 |
| G2 | the refusal text on a non-POSIX OS | `python3 -c "import os,sys; os.name='nt'; sys.argv=['x','ls']; exec(open('$S').read())"` | code 1, the text about WSL, no traceback. **Only the wording of the refusal** is checked: real behaviour under Windows cannot be reproduced this way |
| G3 | `--help` does not crash | `$S --help`, `$S run --help`, `$S interrupt --help` | code 0 |

A prerequisite for G1: `uv` in PATH and either a cached CPython 3.8 or network
access to download it.

---

## Report format

For every case: `ID — PASS/FAIL` and one line of fact (what was actually printed
/ which code). For a FAIL — the verbatim output. Separately at the end: what in
`SKILL.md` diverges from the observed behaviour.
