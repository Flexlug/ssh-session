# Pitfalls

Everything below was actually reproduced, not guessed at.

**`exit` inside `run` destroys the session — it runs in that same shell.** That
is the price of keeping state: `run` works in ssh's login shell, so `exit`,
`logout` or a fragment like `[ -f x ] || exit 1` logs it out and takes the
session with it, along with cwd, venv and background jobs. `run` now **rejects**
such commands (code 2) and offers alternatives; verified that `(exit 42)` and
`sh -c 'exit 7'` still honestly return 42 and 7 with the session alive:

```bash
$S run box '(exit 42)'          # subshell — code 42, session intact
$S run box "sh -c 'exit 7'"     # separate process — code 7
$S run --allow-exit box 'exit'  # if closing the session is the actual goal
```

**`run` requires a POSIX shell at the prompt.** The marker mechanics rely on
`bash`/`zsh`/`sh` (`{ ... }` and `$?` are needed). It will not work in `fish` —
`new` exits with code 4 and suggests `--shell 'bash -i'`. And if a REPL or a TUI
is running in the session, `run` will simply print its wrapper inside that
program — return to the shell first.

**After a timeout the session is marked busy, and `run` checks that.** A command
that did not finish by the timeout keeps reading the pty and eats the first line
of the **next** command's wrapper — garbled output and
`bash: syntax error near unexpected token '}'`. Previously this happened
silently.

The mark is not "sticky": `run` sends a probe and sees whether the shell answers.
If the command has already been dealt with (fed through `send`, answered through
`expect`, or finished on its own), the mark is cleared automatically and work
continues. If the probe is swallowed — refusal with code **126** and the text
"verified: it swallowed a probe". `ls` shows such a session as `busy`, `info`
shows the reason.

```bash
$S read box --tail 20     # look at what is hanging
$S interrupt box          # Ctrl-C plus a check that the shell came back
$S send box --key q       # if it is a full-screen program — its own key
```

`interrupt` does **not** report success blindly: `less`, `top` and others ignore
SIGINT, so after Ctrl-C it probes the shell and on failure exits with 126,
suggesting you send the program's native exit key. Previously it cleared the
mark, and the next `run` printed the wrapper into `less` as keystrokes.

`run --force` bypasses the check, but carefully: if the command really is
hanging, `--force` will **not** return its code — it will sit out the whole
`--timeout` and give back 124 with a raw transcript. The default is 120 seconds
of silence, so set a short `--timeout` explicitly. Auto-answers are disabled with
`--force` in a busy session: otherwise the rules match text drawn by the TUI and
start pressing keys inside it (three Enters into a live `less` — observed).

**`send --key C-c` interrupts the remote command but does not unblock a waiting
`run`.** Ctrl-C tears down the whole `{ ... }` group, so the closing marker is
never executed: `run` sits out its timeout and holds the lock all that time,
giving others a false "busy" although the shell is already free. There is a
separate command for this that does both:

```bash
$S interrupt box     # run returns immediately with code 130, the lock is released
```

**A command that reads stdin eats the marker — and the next command's marker
too.** `run box 'read x'` runs into a timeout (code 124), and the next `run` gets
garbled output like `got=[{ printf '__SSB...']` and
`syntax error near unexpected token '}'`. The session recovers on its own
afterwards, but the cure is this:

```bash
$S run box 'read x < /dev/null; echo "rc=$?"'      # close off stdin
$S run box 'echo hello | { read x; echo "[$x]"; }' # or feed it data
```

**`wait` sees the echo of what you have just sent.** The shell echoes input back,
so `send box 'sleep 3; echo TRAP_WORD'` + `wait box 'TRAP_WORD'` fires instantly
rather than after 3 s. That is why `wait` looks at **new** output only by default
(`--from-start` restores the old behaviour) — and even so: if you need to wait for
a command to finish, that is `run --timeout`'s job, not `wait`'s. `wait` is good
for a line in the log of an already running process.

**Full-screen TUIs are read approximately.** `read` is a line-by-line history,
not a screen snapshot: cursor-positioning escape sequences are stripped, so
`top`'s frames overlay each other and the header can end up glued to a line from
the previous frame. For data, use batch modes (`top -b -n1`,
`journalctl --no-pager`), and keep `read` for what streams line by line anyway:
logs, builds, REPLs.

**One command at a time, but `run` will not always wait.** Distinguish two cases:

- the command is held by a **parallel `run`** — the second one queues on the
  flock and executes after the first (verified: two 5 s `run`s took 10.4 s);
- the command was started through **`send`** — then `run` does not wait but
  refuses immediately with code 126 in ~0.05 s, because it sees the shell is not
  at a prompt.

For real parallelism, use separate sessions — see the section above.

**An unknown host key hangs the connection.** `new` exits with code 4, shows the
tail of the output with the fingerprint question, and suggests the command.
Accepting the key writes to `~/.ssh/known_hosts` — that is a configuration
change, so ask the user before answering `yes`:

```bash
$S send box yes
```

**Sessions do not survive a reboot.** State lives in the directory from the
"Requirements" section — on Linux that is `/run/user/<uid>/sshsess/`, on macOS
`$TMPDIR`. The system cleans both. Override with `SSHSESS_DIR`.

Sessions created before the move to `$TMPDIR` lived in `~/.cache/sshsess` and no
longer appear in the listing. There is nothing to migrate deliberately — a
session is a live ssh connection with a daemon process, and after a reboot it is
dead in any case. But if the old directory is not empty, `ls` mentions it and
suggests the command to look inside.
`out.log` is not rotated — a TUI running for a few minutes inflates it to
megabytes. It is cleared without dropping the connection: `truncate NAME` (which
refuses if a command is executing right now, because that command counts output
by offsets in the file).

**Network drops.** `ServerAliveInterval=15` / `ServerAliveCountMax=4` are set, so
a dead connection is detected in about a minute and the session goes `dead`.
There is no auto-reconnect, but you do not need to reopen it by hand with the
same arguments: `reconnect NAME` takes the target, the ssh arguments, `--shell`
and the pty size from `meta.json`. Verified on a killed ssh process. The remote
state is genuinely lost in the process — a new shell, a new cwd, no variables —
and `reconnect` says so outright.

