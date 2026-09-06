**English** | [Русский](README.ru.md)

# ssh-session

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](skills/ssh-session/scripts/sshsess.py)
[![stdlib only](https://img.shields.io/badge/deps-stdlib%20only-green)](skills/ssh-session/scripts/sshsess.py)
[![Linux · macOS](https://img.shields.io/badge/platform-Linux%20%C2%B7%20macOS-lightgrey)](#limitations)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A skill for agentic harnesses (**Claude Code** first and foremost) that keeps a
live SSH session to a server and runs commands over it. One connection, one
remote shell for the whole job: `cd`, `export`, an activated venv and background
jobs all survive between calls.

A plain `ssh host "cmd"` pays a full handshake per command and starts a **new**
shell every time — so an agent that needs ten steps on a server loses its state
after each one and keeps gluing it back together with one-liners like
`cd /srv/app && source venv/bin/activate && …`.

Measured on a real host: 10 commands over a fresh ssh each time — **25.2 s**,
over a live session — **3.5 s**.

## Why an agent needs this and a human doesn't

A human at a keyboard already has a live session — the terminal. The problem is
that SSH tooling is built for that human: when a `[Y/n]`, a password prompt or a
pager shows up, they press a key. Nobody is there to rescue an agent, and the
typical ending is a command hanging inside `less` until the timeout — or worse,
a wrapper typed **into** a running `top` as keystrokes.

So this is not just "the session stays alive". There are three layers keeping
the work from stalling:

1. **Muting interactivity.** Right after connecting, a non-interactive
   environment is applied (`PAGER=cat`, `DEBIAN_FRONTEND=noninteractive`,
   `EDITOR=false`, …). This closes the nastiest trap — the pager: with
   `--no-harden`, `git log` runs into the timeout because `less` is waiting
   for `q`.
2. **Auto-answers.** Passwords, pagers and "Press ENTER" are answered
   immediately; confirmations that actually change something (`[Y/n]`, a `dpkg`
   conffile conflict) only with an explicit `--yes`. A rule fires **only if the
   match sits at the very end of the output**, i.e. the program really is
   waiting: `echo "Do you want to continue? [Y/n]"` as ordinary output is not
   answered.
3. **Refusing to type into something that is not a prompt.** Before sending,
   the bracketed-paste state in the log already received is inspected — for
   free, without touching the server. If a TUI is open in the session, or a
   command is still running, `run` exits with code 126 and tells you which key
   to press instead of blindly dictating text into `vim`.

On top of that: every refusal has its own exit code (`124` timeout, `125` busy
with a parallel `run`, `126` not at a prompt, `130` interrupted) — an agent can
tell "slow" from "broken" without parsing prose.

## Installation

The skill is the `skills/ssh-session/` directory with a `SKILL.md` following the
[Agent Skills specification](https://agentskills.io); inside it there is only a
stdlib Python script, so installing it amounts to copying a folder. The
repository doubles as a Claude Code plugin marketplace, which gives it a
shorter path.

**Claude Code, as a plugin** (versioned, with `/plugin update`):

```
/plugin marketplace add Flexlug/ssh-session
/plugin install ssh-session@flexlug
```

**By hand, into any harness** — clone and drop the skill folder where it belongs:

```bash
git clone https://github.com/Flexlug/ssh-session /tmp/ssh-session
cp -r /tmp/ssh-session/skills/ssh-session ~/.claude/skills/
```

| Harness | Where it goes |
|---|---|
| Claude Code, Claude Desktop | `~/.claude/skills/` (or `.claude/skills/` in a project) |
| Codex CLI | `~/.codex/skills/` |
| Gemini CLI | `~/.gemini/skills/` |
| GitHub Copilot / VS Code | `~/.config/skills/` (or `.github/skills/` in a repository) |
| Cursor, OpenCode, Goose, Amp | see the client's docs — they all read `SKILL.md` |

There is also the universal `gh skill install Flexlug/ssh-session` (GitHub CLI
2.90+), but I have not used it — I have not verified that it works against this
repository.

Check the driver is alive (it runs perfectly well on its own, without any agent):

```bash
~/.claude/skills/ssh-session/scripts/sshsess.py ls
# no sessions
```

## Requirements

`python3` (3.8+) and `ssh` — nothing else. No pip packages, no tmux, no Node.

On the server side the dependency is not the distribution but the shell:

| Needed for | Requirement | Where it breaks |
|---|---|---|
| `run` (markers, `$?`) | any POSIX shell | `fish`, `csh` → fixed by `--shell 'bash -i'` |
| layer 3 (busy check) | bracketed paste, i.e. readline/zle | `dash`, `ash`/busybox → the check answers "unknown" and does not protect |

## Quick start

```bash
S=~/.claude/skills/ssh-session/scripts/sshsess.py   # a plugin install puts it elsewhere

$S new box myhost.example.com    # open a session named box
$S run box uname -sr             # run it, get the output and the exit code
$S read box --tail 40            # look at what is going on in there
$S send box --key C-c            # send raw input
$S kill box                      # close it
```

State persists — that is the entire point:

```bash
$S run box 'cd /etc && export MYVAR=hello'
$S run box 'pwd; echo "MYVAR=$MYVAR"'
# /etc
# MYVAR=hello
```

`run` returns **clean** output (no prompt, no echo) and the **real** exit code
of the remote command:

```bash
$S run box 'ls /nope'; echo "rc=$?"
# ls: cannot access '/nope': No such file or directory
# rc=2
```

A session outlives the process that created it: do whatever you like between
calls, it stays where it was.

## Commands

```bash
# --- sessions ---
$S new NAME TARGET [--timeout 40] [--shell 'bash -i'] [--force] [--no-harden] [-- SSH_ARGS...]
$S ls | info NAME | reconnect NAME | truncate NAME | kill NAME|--all | prune

# --- execution ---
$S run [--timeout 120] [--yes] [--no-auto] [--force] [--allow-exit] NAME CMD...
$S expect NAME [--once] [--yes] [--dry-run] [--list-rules]
$S send NAME [TEXT] [--key KEY] [--no-enter] [--paste] [--wait SEC]
$S interrupt NAME
$S read NAME [--tail N] [--all] [--since OFFSET] [--raw] [--no-filter]
$S wait NAME REGEX [--timeout 60] [--from-start]
```

What to use for what:

| Task | Tool |
|---|---|
| Run a command, get output and an exit code | `run` — almost always this one |
| Answer a prompt (sudo, host key, `yes/no`) | `send` |
| Work in a REPL (`python3`, `psql`, `mysql`) | `send` + `read` |
| Unstick a session that is stuck | `expect`, `interrupt` |
| Wait for a line in the output of an already running process | `wait` |

REPLs are supported (`--paste` for multi-line input — it sidesteps PyREPL's
auto-indent), TUIs through keys (`send --key Down --key Enter` picks an entry in
a whiptail menu), and any number of parallel sessions, including several to the
same host.

## Passwords

They live in `~/.config/sshsess/secrets.json`, mode strictly `0600` — otherwise
`sshsess` refuses to run. The password is never passed into the chat, never ends
up on a command line, and is not written to the session log (password prompts
turn terminal echo off); the report prints `<secret:sudo>` in place of the value.

```bash
mkdir -p ~/.config/sshsess
printf '%s\n' '{"secrets": {"sudo": "…"}}' > ~/.config/sshsess/secrets.json
chmod 600 ~/.config/sshsess/secrets.json
```

Custom auto-answer rules go into `~/.config/sshsess/rules.json` (a regexp plus
exactly one of `send` / `key` / `secret`; the `confirm` flag ties a rule to
`--yes`).

**Accepting a host key is deliberately not automated** — that is a security
decision and it belongs to a human: `new` exits with code 4 and shows the
fingerprint.

## Limitations

- **Windows is not supported**, and that is a decision rather than an open debt:
  there is no pty and no `fork()` there, and ConPTY would require a third-party
  dependency such as `pywinpty`, breaking the main property — stdlib only,
  nothing to install. The script exits with code 1 and a clear message. WSL
  covers Windows: inside it this is ordinary Linux and everything works as is.
- **Sessions do not survive a reboot.** State lives in `$XDG_RUNTIME_DIR/sshsess`
  (Linux) or `$TMPDIR` (macOS) — the system wipes both. Override with
  `SSHSESS_DIR`.
- **There is no auto-reconnect.** A dropped link is detected in about a minute
  (`ServerAliveInterval=15`) and the session goes `dead`; `reconnect NAME`
  reopens it with the same arguments, but the remote state is genuinely gone —
  and `reconnect` says so outright.
- **A session name is a machine-wide resource.** Anyone using the same name lands
  in the same shell; `ls`/`info` show the owner, and `new` refuses a name held by
  a live session. Real isolation comes from `SSHSESS_OWNER`.
- **Full-screen TUIs read approximately**: `read` is a line-by-line history, not
  a screen snapshot. For data, use batch modes (`top -b -n1`,
  `journalctl --no-pager`).

The client side is tested on Linux; macOS is untested, but the parts specific to
it are covered explicitly — the choice of state directory, `0700`/`0600` modes,
and the AF_UNIX path length limit (104 bytes there against 108 on Linux; checked
on an emulation of the same length: 104 fails with a clear message rather than
"daemon failed to start").

## Tests

The test stand needs no remote host at all: `tests/local-sshd.sh` brings up a
temporary `sshd` on 127.0.0.1 — no sudo, no system-wide changes, `~/.ssh` is
never touched, everything lives in one work directory and is deleted with it.

```bash
cd skills/ssh-session
bash tests/local-sshd.sh start     # prints the target to connect to
bash tests/local-sshd.sh stop      # kills it and removes the work directory
```

The smoke set is in [`tests/TESTCASES.md`](skills/ssh-session/tests/TESTCASES.md).

## How it works

The daemon is a double-forked process that owns the pty master and appends every
byte coming from the server to `out.log`. Clients read that file directly, so
`read` works even while a command is still streaming. Only side-effecting
operations go through the unix socket: `send` / `info` / `kill`.

`run` rests on two tricks, and both are mandatory:

1. The marker literals are split by quotes (`'__SS''B...'`), so the **echo** of
   the line we send does not contain the text we are searching for. Otherwise the
   search would always hit the echo and cut the output in the wrong place.
2. Everything is wrapped in a single `{ ... }` group. The shell will not start
   executing until it has read the closing brace, so all the echo ends up
   **before** the first marker's output — that is, outside the region being cut.

The full description — pitfalls, the diagnostics table and the exit codes — is in
[`SKILL.md`](skills/ssh-session/SKILL.md). That is the very file the agent reads.

## Authors

Developed as a pair: **Flexlug** — idea, requirements, testing;
**Claude (Anthropic)** — implementation, shakedown on live hosts, documentation.
Co-authorship is reflected in the commits.

Licensed under [MIT](LICENSE).
