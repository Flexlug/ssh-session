# Interactive work: REPLs, prompts, TUIs

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

