---
name: ssh-session
description: Keeps a live SSH session to a server and runs commands over it without a fresh handshake each time — cwd, exported vars, venvs and background jobs persist between calls. Use this skill whenever you need to run more than one command on a remote host, work through a server step by step, connect over SSH, set up or deploy something on a VPS, answer interactive prompts (sudo, host key, REPL, TUI) or read the output of a long-running command. Triggers - ssh, connect to the server, log in to the server, run this on the server, remote shell, VPS, deploy to a server, persistent ssh session, run commands on host.
---

# Persistent SSH sessions

A plain `ssh host "cmd"` pays a full handshake per command and starts a **new**
shell every time — so `cd`, `export`, an activated venv and background jobs are
all lost between calls. This skill keeps one connection alive in a background
daemon that owns a pty, and sends commands into the **same** shell.

Measured on a real host: 10 commands over a fresh ssh each time — **25.2 s**,
over a live session — **3.5 s**.

The driver is `scripts/sshsess.py` next to this file — executable, runnable
directly (Python 3, stdlib only: no tmux, no pip packages). Its full path
depends on how the skill was installed: a manual install puts it under
`~/.claude/skills/ssh-session/scripts/`, a plugin install under
`~/.claude/plugins/`. Locate it once and keep the path in your reply so you do
not have to search again:

```bash
find ~/.claude ~/.config .claude -path '*ssh-session/scripts/sshsess.py' 2>/dev/null | head -1
```

In the examples below the driver is shortened to `$S`; set `S=...` at the start
of the same Bash call — variables do not survive between separate calls, so
there you have to substitute the full path. If your local shell is fish, the
assignment differs: `set S ...`.

## Requirements

`python3` and `ssh` — nothing else. No pip packages, no tmux, no Node.

Verified on Python 3.8 — the full `new`/`run`/`kill` cycle, including parsing of
`-- ssh arguments`, so any interpreter from 3.8 up will do.

**The reference platforms are Linux and macOS. Windows is not supported**, and
that is a decision rather than an open debt: there is no pty and no `fork()`
there, and ConPTY would require a third-party dependency such as `pywinpty`,
breaking the skill's main property — stdlib only, nothing to install. The script
exits with code 1 and a clear message. WSL covers Windows: inside it this is
ordinary Linux and everything works as is.

The same follows for what **not** to do: do not replace the unix socket with a
file-based channel, and do not add a pty-less mode. Neither idea gets any closer
to Windows — the obstacle is not the channel, it is the pty and `fork()`.

**On the client side** this is tested on Linux; macOS is untested (no machine to
test on), but the parts specific to it are covered:

- **State directory.** The order is: `SSHSESS_DIR` → `$XDG_RUNTIME_DIR` (Linux)
  → `$TMPDIR` on macOS → `~/.cache/sshsess`. On macOS `$TMPDIR` is a private
  per-user `0700` directory that the system cleans, i.e. the closest analogue of
  `XDG_RUNTIME_DIR`. The persistent `~/.cache` is left only as the last fallback:
  a password can land in `out.log` if a remote prompt forgot to turn echo off,
  and such a file has no business staying on disk forever.
- **Permissions.** Directories `0700`, files `0600` — verified in every location,
  including upgrading a directory created by an older version with `0755`.
- **The AF_UNIX path length** there is 104 bytes against 108 on Linux. With a
  real macOS prefix (`/var/folders/xx/<hash>/T/sshsess`, ~50 bytes) that leaves
  ~45 characters for the session name. Verified on an emulation of the same
  length: 101 bytes works, 104 refuses with a clear message rather than "daemon
  failed to start".

The test stand is portable too: `sshd` is looked up in several paths including
Homebrew, and port occupancy is checked through Python rather than the
Linux-only `ss`.

**On the server side** the dependency is not the distribution but the shell:

| Needed for | Requirement | Where it breaks |
|---|---|---|
| `run` (markers, `$?`) | any POSIX shell | `fish`, `csh` → fixed by `--shell 'bash -i'` |
| layer 3 (busy check) | bracketed paste, i.e. readline/zle | `dash`, `ash`/busybox → the check answers "unknown" and does not protect |

The wrapper construct is verified in busybox `ash` — markers, `$?` and the
trailing `&` all work, so the core is portable there as well. The muting
variables (`DEBIAN_FRONTEND`, `NEEDRESTART_MODE`) are Debian-specific, but on
other systems they are simply unknown environment variables — no harm done. The
confirmation patterns cover `apt`, `dnf`, `pacman` and `zypper`.

For checks there is a local stand that needs no remote host at all:
`tests/local-sshd.sh start` brings up a temporary sshd on 127.0.0.1 (no sudo, no
system-wide changes, `~/.ssh` is never touched), and `tests/TESTCASES.md` is the
smoke set.

## Quick start

```bash
S=/path/to/ssh-session/scripts/sshsess.py   # see above; in fish: set S ...

$S new box server.example.com     # open a session named box
$S run box uname -sr              # run it, get the output and the exit code
$S read box --tail 40             # look at what is going on in there
$S send box --key C-c             # send raw input
$S kill box                       # close it
```

A session lives independently of the process that created it: do whatever you
like between calls, it stays where it was. Verified — the examples below were
driven by dozens of separate Bash calls over half an hour.

## What to use for what

This is the main decision when working with the skill:

| Task | Tool |
|---|---|
| Run a command, get output and an exit code | `run` — almost always this one |
| Answer a prompt (sudo, host key, `yes/no`) | `send` |
| Work in a REPL (`python3`, `psql`, `mysql`) | `send` + `read` |
| Wait for a line in the output of an already running process | `wait` |
| See what is on the "screen" right now | `read` |

`run` is the workhorse. It wraps the command in a `{ ... }` group with two
markers, so it returns **clean** output (no prompt, no echo) and the **real**
exit code of the remote command:

```bash
$S run box 'ls /nope-does-not-exist'; echo "rc=$?"
# ls: cannot access '/nope-does-not-exist': No such file or directory
# rc=2
```

State persists — that is the entire point:

```bash
$S run box 'cd /etc && export MYVAR=hello'
$S run box 'pwd; echo "MYVAR=$MYVAR"'
# /etc
# MYVAR=hello
```

Output is byte-exact: 20,000 lines through a session give the same md5 as
locally, and a 3000-character line is not broken up (the pty is 200 columns wide
and wraps nothing — wrapping is the terminal emulator's job, and there is no
emulator here).

## Commands

```bash
# --- sessions ---
$S new NAME TARGET [--timeout 40] [--shell 'bash -i'] [--force] [--no-harden] [-- SSH_ARGS...]
$S ls                      # all sessions: live/dead, host, owner, log size
$S info NAME               # pid, uptime, owner, log path (works for a dead one too)
$S reconnect NAME          # reopen with the same ssh arguments and --shell
$S truncate NAME           # clear a live session's log without dropping the connection
$S kill NAME | --all
$S prune                   # forget dead sessions along with their logs

# --- execution ---
$S run [--timeout 120] [--yes] [--no-auto] [--force] [--allow-exit] NAME CMD...
$S expect NAME [--once] [--yes] [--dry-run] [--list-rules] [--timeout 120]
$S send NAME [TEXT] [--key KEY] [--no-enter] [--paste] [--wait SEC]
$S interrupt NAME          # Ctrl-C to the remote command AND unblock a waiting run
$S read NAME [--tail N] [--all] [--since OFFSET] [--raw] [--offset] [--no-filter]
$S wait NAME REGEX [--timeout 60] [--from-start] [--since OFFSET]
```

`run`'s `--timeout` goes **before** the session name: everything after the name
is passed to the remote command as a whole, so that its own flags are not eaten
by argparse (`$S run box ls -la` works as expected).

Extra ssh arguments go after `--`, and are inserted **before** the host
(otherwise ssh treats them as the remote command):

```bash
$S new box 203.0.113.10 --timeout 12 -- -p 2222 -l deploy
```

If the login shell on the host is not POSIX (fish, csh), `run` will not be able
to work — set the shell explicitly. The same cures logins that stall on terminal
queries:

```bash
$S new box myhost --shell 'bash -i'
```

`read` reads like a transcript of the session: commands sent through `run` show
up with a `> ` prefix (those are continuation lines from the wrapper), while
those sent through `send` appear as ordinary echo with a prompt, without the
prefix. `run`'s internal markers are hidden (`--no-filter` shows them, `--raw`
gives the raw stream with escape sequences).

Keys for `send --key`: `C-c`, `C-d`, `C-z` (any `C-<letter>`), `Enter`, `Tab`,
`Escape`, `Up`/`Down`/`Left`/`Right`, `Home`, `End`, `PgUp`, `PgDn`, `Space`,
`BSpace`. The flag is repeatable.

### Interactive work: REPLs

```bash
$S send box 'python3 -q'
$S wait box '>>>'
$S send box '2+2' --wait 1.5      # send, then after 1.5 s show what came back
# 2+2
# 4
$S send box --key C-d             # leave the REPL
```

Send multi-line input **with `--paste`**. Modern REPLs (PyREPL in Python 3.13+)
add indentation to every continuation line themselves, so a plain `send` stacks
your indentation on top of theirs and the block dies with `IndentationError`.
`--paste` wraps the text in bracketed paste, and it is accepted as is. An
unclosed block (`...` instead of `>>>`) is closed by an **empty line** — that is
a `send` with no text:

```bash
$S send box --paste 'def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
'
$S send box '' --wait 1            # an empty line closes the block
$S send box 'fib(30)' --wait 2     # -> 832040
```

In `read` such a session looks ragged (`>>> f>>> fi>>> fib…`): PyREPL redraws
the line by moving the cursor rather than with `\r`, and gluing that back
together is impossible without a full terminal emulator. It does not affect the
result — only readability.

## Several sessions at once

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

## Keeping the work from stalling

SSH tooling is built for a human at a keyboard. Nobody is there to rescue an
agent, so there are two layers here: prevent the prompt from appearing, and if
it did appear, answer it.

### Layer 1: muting interactivity (on by default)

Right after connecting, `new` applies a non-interactive environment and prints
`(non-interactive env applied)`. Disabled with `--no-harden`.

```
PAGER=cat GIT_PAGER=cat SYSTEMD_PAGER=cat LESS=FRX MANPAGER=cat
DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a APT_LISTCHANGES_FRONTEND=none
EDITOR=false VISUAL=false GIT_TERMINAL_PROMPT=0
LC_ALL=C.UTF-8   (only if such a locale exists — otherwise unicode breaks)
```

This closes the nastiest trap — **the pager**. Verified: with `--no-harden`,
`run box 'git log'` runs into timeout 124 because `less` is waiting for `q`; with
muting on, the same command and `systemctl status` go through normally. `man`,
`journalctl` and `git diff` belong to the same family — all of them silently wait
for a human. `EDITOR=false` means `git commit` without `-m` honestly fails
instead of hanging the session in `vim`.

### Layer 2: auto-answers

`run` watches the output and answers known prompts without waiting for a human.
What was answered is written to stderr, so it is visible in the work log too.

```bash
$S run box 'sudo apt-get install -y nginx'      # the password is substituted from a file
$S run box --yes 'apt-get upgrade'              # + [Y/n] confirmations
$S run box --no-auto 'something delicate'       # answer nothing
```

The split is deliberate: **passwords, pagers and "Press ENTER" are answered
immediately**, while confirmations that change something (`[Y/n]`, `Proceed?`, a
`dpkg` conffile conflict) only with `--yes`. An accidental `y` does not roll
back.

The key property that makes this safe: a rule fires **only if the match sits at
the very end of the output**, i.e. the program really is waiting. Without that
condition a pattern once caught the help text inside `less` and sent Enter three
times into someone else's TUI — and Enter on a highlighted menu entry can
confirm anything at all. Verified: `echo "Do you want to continue? [Y/n]"` as
ordinary output is not answered, a real prompt is.

### Passwords

They live in `~/.config/sshsess/secrets.json`, mode strictly `0600` — otherwise
`sshsess` refuses to run. The password is never passed into the chat and never
ends up on a command line.

```bash
mkdir -p ~/.config/sshsess
cat > ~/.config/sshsess/secrets.json <<'EOF'
{"secrets": {"sudo": "…", "db": "…"}}
EOF
chmod 600 ~/.config/sshsess/secrets.json
```

Verified: the password is not in the session log — neither as text nor in the
raw bytes (password prompts turn terminal echo off). The report prints
`<secret:sudo>` in place of the value. If the required secret is missing,
`sshsess` says so explicitly instead of hanging.

### An already stuck session, and TUIs

```bash
$S expect box --dry-run --once     # what is it waiting for? without sending anything
$S expect box --once --yes         # answer and unblock
$S expect box --list-rules         # which rules are in effect
```

`expect` also looks backwards through the log, so it catches a prompt printed
**before** it was started — and that is the main scenario. Verified on a stuck
`[Y/n]` and on a password request.

For TUIs, send keys directly. A single printable character is a key too, and is
sent without Enter (`q` in `less`/`top`, `y` in a dialog):

```bash
$S send box --key Down --key Enter    # pick a whiptail menu entry
$S send box --key q                   # leave the pager
```

Verified on a real `whiptail --menu`: `Down` + `Enter` selected the second entry.
There are `F1`-`F12`, arrows, `Home`/`End`/`PgUp`/`PgDn`, and any `C-<letter>`.

A single key is sent without a newline, so it stays sitting on the input line —
but the next `run` will not break because of it: the wrapper starts with Ctrl-U,
which discards unfinished input. Verified both with a leftover `q` and with a
whole line without Enter — previously that produced `bash: q{: command not found`
plus `syntax error`.

### Custom rules

```json
{
  "extend_defaults": true,
  "rules": [
    {"label": "mysql-pw", "pattern": "Enter password:", "secret": "db"},
    {"label": "my-installer", "pattern": "Continue\\? \\(yes/no\\)", "send": "yes",
     "confirm": true},
    {"label": "dialog-ok", "pattern": "<OK>\\s*$", "key": "Enter"}
  ]
}
```

Goes into `~/.config/sshsess/rules.json` or is passed with `--rules FILE`.
Fields: `pattern` (a regexp) and exactly one of `send` / `key` / `secret`;
`confirm: true` makes the rule depend on `--yes`; `extend_defaults` adds the
rules to the built-in ones instead of replacing them.

### Layer 3: do not type into something that is not a prompt

`run` used to print its wrapper into a running `top` or `less` as **keystrokes** —
`less` was observed answering `Pattern not found` and `There is no - option`, and
in `vim` you can write a file that way. Now `run` first checks whether the shell
is reading commands, and on refusal says which key to press.

The evidence is free — the bracketed-paste state in the log already received, no
request to the server and no byte sent. `readline` turns it on (`?2004h`) while
the shell is reading a line, and turns it off (`?2004l`) before launching any
command. Measured: bash at a prompt → `h`; `less`, `top`, `whiptail` → `l`; after
leaving `less` → `h` again. On top of that, the switch to the alternate screen
used by `whiptail` is detected separately — then the message names the
full-screen program outright.

One check covers two cases at once: a TUI started through `send` (there is no
timeout there, so the busy mark was not set) and a command that simply has not
finished. The exit code is **126**.

Limits worth knowing:

- **A REPL with line-by-line input** (`python3`, `psql`) turns bracketed paste on
  itself, so it reads as "a prompt", and `run` will type into it after all. This
  is less dangerous — the text will be evaluated as an expression and produce an
  error — but it is still better to return to the shell first.
- **A shell without bracketed paste** (`dash`, `sh`, `bash --noediting`) leaves no
  evidence: the check honestly answers "unknown" and does not block the work. But
  it does not protect either: the probe only fires if the busy mark is set, and a
  TUI started through `send` does not set it. On such shells layer 3 simply does
  not work — verified on `bash --noediting`, the wrapper went into `top`.
- **The evidence can be forged**: a command printing `ESC[?2004h` into its own
  output looks like a returned prompt. That is why the busy mark is cleared by
  the probe, not by the log — the log alone cannot clear it.
- **A stuck alternate screen** (a TUI killed with SIGKILL before `rmcup`) no
  longer hangs the session forever: the latest evidence by position wins, so a
  prompt printed after that overrides the stale flag.
- **`truncate` refuses while a TUI is running** (code 126): the log is the only
  evidence, and erasing it means removing the protection.
- The check sits **inside** the lock: outside it, a second parallel `run` would
  see the first one's command executing and fail with 126 instead of waiting its
  turn.

### What automation does not solve

- **A plain `less` with no flags** shows the file name in inverse video rather
  than text: once escape sequences are stripped this is indistinguishable from
  output, and no rule will catch it — you get 124, and the way out is
  `send --key q`. What is caught: `(END)`, `--More--` and `lines N-M/T`
  (including `less -M`, where the line ends with a percentage). Muting makes this
  almost irrelevant — the pager does not start at all.
- **`expect --list-rules`** shows all rules, including those gated behind
  `--yes` — they are marked `[needs --yes]`.
- **A prompt with no secret for it** is printed as a separate
  `PROMPT NOT ANSWERED` line rather than as "auto-answered": nothing was sent,
  and the answer count is not spoiled by it.
- **`run` into a running TUI is no longer destructive — it refuses.** See below.
- **Accepting a host key is deliberately not automated** — that is a security
  decision and it belongs to a human.

## Pitfalls

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

## Diagnostics

| Symptom | What it means and what to do |
|---|---|
| `session 'x' is not running (no live socket ...)`, code 3 | There is no such session, or it died. `ls` shows the status, `new` opens it again. |
| `could not be opened` + a tail of ssh output, code 3 | ssh failed immediately. The message contains the full launch line and ssh's output — read it: `unknown option`, `No address associated with hostname`, `Connection refused`. |
| `started but the remote shell did not respond`, code 4 | The connection is waiting on input — a host key or a password. The output tail is in the message; answer through `send`. |
| `command still running after Ns`, code 124 | The command did not finish. The output above is partial. Investigate with `read`, interrupt with `interrupt`. |
| `session 'x' is busy`, code 125 | A parallel `run` holds the lock. Give a bigger `--timeout` or work through a separate session. |
| `still has a command running`, code 126 | Verified by probe: the previous command is eating input. `interrupt`, or the program's native exit key. |
| `sent Ctrl-C … still not reading`, code 126 | The program ignores SIGINT (`less`, `top`). Send its exit key: `send NAME --key q`. |
| `refusing to run … not at a prompt`, code 126 | Something is executing in the session, or a TUI is open. `read --tail 20`, then `interrupt` or the native exit key. |
| `refusing to run … full-screen program`, code 126 | A full-screen program (`whiptail`, `dialog`). Leave it with its key — usually `Enter` or `q`. |
| `died while running the command`, code 3 | The connection dropped mid-command; the message carries the log tail. |
| Output contains `__SSB...` / `syntax error near '}'` | A program reading stdin ate the marker. See the pitfalls. |
| `refusing to run this: 'exit' would log out...`, code 2 | The command would close the session. Wrap it in `( ... )` or `sh -c`. |
| `session 'x' is already running`, code 2 | The name is held by a live session. Take another one — it may belong to a neighbouring agent. |

Exit codes: `0`/the remote command's code, `2` — argument error (and the `exit`
guard's refusal), `3` — the session is dead or absent, `4` — connected but the
shell does not answer, `124` — timeout, `125` — the lock is held by a parallel
`run`, `126` — an unfinished command is hanging in the session, `130` —
interrupted through `interrupt`.

If something is truly strange, look at the raw stream without filtering and at
the daemon's own log:

```bash
$S read box --raw | tail -c 2000
cat /run/user/1000/sshsess/box/daemon.log
```

## How it works

The daemon is a double-forked process that owns the pty master and appends
everything coming from the server to `out.log`. Clients read that file directly,
so `read` works even while a command is still streaming output. Only
side-effecting operations go through the unix socket: `send` / `info` / `kill`.

`run` rests on two tricks, and both are mandatory:

1. The marker literals are split by quotes (`'__SS''B...'`), so the **echo** of
   the line we send does not contain the text we are searching for. Otherwise the
   search would always hit the echo and cut the output in the wrong place.
2. Everything is wrapped in a single `{ ... }` group. The shell will not start
   executing until it has read the closing brace, so all the echo (including the
   `>` continuation lines) ends up **before** the first marker's output — that is,
   outside the region being cut. The command sits on its own physical line for
   this: `foo & ; printf` is a syntax error, while `foo &` plus a newline is not,
   which is why `run box 'sleep 30 &'` works.
