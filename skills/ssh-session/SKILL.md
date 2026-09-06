---
name: ssh-session
description: Run commands on a remote host over a persistent SSH session — one live connection per named session, so cwd, exported vars, activated venvs and background jobs survive between tool calls. Use this instead of `ssh host "cmd"` in Bash - a bare ssh call pays a full handshake, starts a fresh shell that loses all state, and cannot answer an interactive prompt. Applies whenever a task touches a remote host - ssh, connect or log in to the server, run this on the server, VPS, deploy, remote shell - and especially for more than one command, sudo or host-key prompts, REPLs (python3, psql, mysql), TUIs, or reading the output of a long-running command.
---

# Persistent SSH sessions

**Do not run `ssh host "cmd"` from Bash for this work.** Every such call pays a
full handshake and starts a **new** shell, so `cd`, `export`, an activated venv
and background jobs are all lost between calls, and there is nobody to answer a
sudo password or a host-key question — the call just hangs until it times out.

This skill keeps one connection alive in a background daemon that owns a pty and
sends commands into the **same** shell. Measured on a real host: 10 commands
over a fresh ssh each time — **25.2 s**, over a live session — **3.5 s**.

Use it whenever the task touches a remote host at all. A single trivial
read-only command is the only case where plain `ssh` is still fine; the moment
there is a second command, state to keep, or a prompt to answer, use a session.

## Locate the driver

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

`python3` and `ssh` on the client — nothing else, no pip packages, no tmux, no
Node. On the server the dependency is not the distribution but the shell: `run`
needs any POSIX shell (`fish`/`csh` are fixed with `--shell 'bash -i'`).

**Linux and macOS only — Windows is not supported** (no pty, no `fork()`); WSL
covers it and works as ordinary Linux. Details, the state-directory order and
the server-side matrix: `references/platform.md`.

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

## Rules that save a session

- **Never `exit` inside `run`** — it runs in that same shell and takes the
  session with it. `run` rejects such commands (code 2); use `(exit 42)` or
  `sh -c 'exit 7'`, or `--allow-exit` if closing really is the goal.
- **Name the session after the task**, not `box` — `deploy-web`, `db-migrate`.
  Names are a machine-wide resource shared with neighbouring agents.
- **Accepting a host key is never automated** — it changes `~/.ssh/known_hosts`,
  so ask the user before sending `yes`.
- **Interactivity is muted by default** (`PAGER=cat`, `DEBIAN_FRONTEND`,
  `EDITOR=false`, …) so `git log` and `systemctl status` do not hang on a pager.
- **Sudo passwords come from `~/.config/sshsess/secrets.json`** (mode `0600`) and
  never touch the chat or a command line. Confirmations that change something
  need `--yes`.
- **A command reading stdin eats the marker** — close it off:
  `run box 'read x < /dev/null'`.

## Going deeper

Read the reference file for the situation you are actually in — do not read them
all up front:

| File | When to open it |
|---|---|
| `references/interactive.md` | REPLs, sudo/`[Y/n]` prompts, passwords, TUIs, `expect`, custom rules, an already stuck session |
| `references/parallel.md` | Several sessions at once, subagent flows, session ownership, lock behaviour |
| `references/pitfalls.md` | Something behaved unexpectedly — timeouts, code 124/125/126, garbled markers, reconnects |
| `references/troubleshooting.md` | You have an error message or an exit code and want the meaning |
| `references/platform.md` | macOS/Windows/WSL specifics, state directory, server-side shell matrix, the local test stand |
| `references/internals.md` | You are changing the driver and need the marker/pty mechanics |
