#!/usr/bin/env python3
"""
sshsess -- persistent SSH sessions you can drive programmatically.

One long-lived ssh connection per named session, held open by a small
background daemon that owns a pty. Every command goes to the *same* remote
shell, so cwd, exported vars, activated venvs and background jobs persist
between calls -- unlike `ssh host cmd`, which pays a full handshake and
starts a fresh shell each time.

Design notes (for whoever has to debug this):

  * The daemon is a double-forked grandchild. It owns the pty master and
    appends every byte the remote writes to <session>/out.log. Clients read
    that file directly -- no read path through the socket, so `read` works
    even while a command is streaming.
  * The unix socket carries only side-effecting ops: send / info / kill.
    One JSON object per connection, newline-terminated, one reply, close.
  * `run` is layered on top of `send` + polling out.log for sentinels. The
    sentinel literals are written split ('__SS''B...') so the shell's own
    echo of the printf line cannot be mistaken for the marker's output.
    That is what lets us return clean output plus a real exit code from an
    interactive shell whose prompt we know nothing about.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import selectors
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from typing import NoReturn

# Everything here rests on POSIX: a pty, fork(), and an AF_UNIX socket. Say so
# in one clear line rather than letting `import termios` raise a traceback that
# makes it look like the script is broken.
if os.name != "posix":
    sys.stderr.write(
        "sshsess: needs a POSIX system (pty, fork, unix sockets).\n"
        f"  Detected os.name={os.name!r}, sys.platform={sys.platform!r}.\n"
        "  On Windows, run this from WSL.\n"
    )
    raise SystemExit(1)

import fcntl  # noqa: E402  -- POSIX-only, imported after the guard above
import pty  # noqa: E402
import termios  # noqa: E402

POLL_FAST = 0.02
POLL_SLOW = 0.25
DEFAULT_COLS = 200
DEFAULT_ROWS = 50
CONNECT_TIMEOUT = 40.0
EXPECT_LOOKBACK = 8192  # bytes of existing output `expect` considers

# ssh options that keep a long-lived session from silently rotting. They are
# defaults, not policy: anything after `--` on `new` is appended and wins.
# Sent once, right after the shell answers. An agent cannot be rescued by a
# human leaning over the keyboard, so the cheapest win is to stop the remote
# side from asking in the first place: no pagers waiting for `q` (this is what
# silently swallows `git log` and `systemctl status`), no editor opening on
# `git commit`, no debconf dialogs, and English messages so prompt patterns
# match. C.UTF-8 only if it exists -- plain C would mangle non-ASCII output.
HARDEN_SCRIPT = (
    "export PAGER=cat GIT_PAGER=cat SYSTEMD_PAGER=cat SYSTEMD_PAGERSECURE=0 "
    "LESS=FRX MANPAGER=cat; "
    "export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a "
    "APT_LISTCHANGES_FRONTEND=none; "
    "export EDITOR=false VISUAL=false GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=; "
    "export PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1; "
    "if locale -a 2>/dev/null | grep -qix 'c.utf-\\?8'; then export LC_ALL=C.UTF-8; fi"
)

SSH_KEEPALIVE = [
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    "-o", "TCPKeepAlive=yes",
]


# ----------------------------------------------------------------- paths ---

LEGACY_ROOT = Path("~/.cache/sshsess").expanduser()


def state_root() -> Path:
    """Where session state lives, in order of preference.

    The log can contain whatever the remote printed -- including a password, if
    a badly written prompt forgot to turn echo off -- so this wants a private,
    volatile directory rather than a permanent one on disk. Linux has
    $XDG_RUNTIME_DIR (0700, wiped at logout); macOS has no such variable, but
    its per-user $TMPDIR (/var/folders/.../T/) is the same idea: 0700 and
    periodically cleaned. ~/.cache is the last resort only, because it survives
    forever.
    """
    env = os.environ.get("SSHSESS_DIR")
    if env:
        return Path(env).expanduser()
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isdir(rt):
        return Path(rt) / "sshsess"
    if sys.platform == "darwin":
        tmp = os.environ.get("TMPDIR")
        if tmp and os.path.isdir(tmp):
            return Path(tmp) / "sshsess"
    return LEGACY_ROOT


def sdir(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        die(f"invalid session name {name!r}: use letters, digits, . _ -")
    return state_root() / name


def paths(name: str):
    d = sdir(name)
    return d, d / "sock", d / "out.log", d / "meta.json"


def owner_id() -> str:
    """Who is driving.

    Verified limitation: subagents inherit CLAUDE_CODE_SESSION_ID unchanged --
    a subagent's environment is byte-identical to its parent's -- so this can
    only ever distinguish *chats*, never parallel agents inside one chat. An
    orchestrator that wants per-agent ownership has to say so explicitly by
    setting SSHSESS_OWNER, which wins over everything else.
    """
    explicit = os.environ.get("SSHSESS_OWNER", "").strip()
    if explicit:
        return explicit
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "") or f"pid:{os.getppid()}"


def error_tail(name: str, lines: int = 12) -> str:
    """Last few meaningful lines of a session, for error messages.

    Raw log tails are unreadable in a failure message: they carry every
    previous command plus the run plumbing. Strip both and keep it short.
    """
    text = clean(read_log(name))
    keep = [l for l in text.split("\n") if not PLUMBING_RE.search(l)]
    while keep and not keep[-1].strip():
        keep.pop()
    return "\n".join(keep[-lines:])


class Busy(Exception):
    pass


def busy_flag(name: str) -> Path:
    return sdir(name) / "busy"


def mark_busy(name: str, reason: str):
    """Remember that a command was left running in the remote shell.

    A timed-out command keeps reading the pty, so it swallows the first line
    of the *next* command's wrapper. The observed result is corrupted output
    plus `bash: syntax error near unexpected token '}'`, and the caller has no
    idea why. Recording it lets the next `run` refuse instead of misbehaving.
    """
    try:
        busy_flag(name).write_text(reason)
    except OSError:
        pass


def clear_busy(name: str):
    try:
        busy_flag(name).unlink()
    except OSError:
        pass


def busy_reason(name: str) -> str:
    try:
        return busy_flag(name).read_text().strip()
    except OSError:
        return ""


class SessionLock:
    """Serialize `run` against one session.

    A remote shell executes one thing at a time, so two overlapping runs
    interleave their wrapper lines and each returns the other's text. Parallel
    subagents sharing a session name hit this immediately, so runs queue on an
    flock instead of corrupting each other. `send` deliberately does not take
    this lock -- Ctrl-C has to land while a run is in flight.
    """

    def __init__(self, name: str, deadline: float):
        self.path = sdir(name) / "lock"
        self.deadline = deadline
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        delay = 0.02
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() >= self.deadline:
                    self.fh.close()
                    raise Busy()
                time.sleep(delay)
                delay = min(0.2, delay * 1.5)

    def __exit__(self, *_exc):
        fh, self.fh = self.fh, None
        if fh is None:
            return False
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
        return False


def die(msg: str, code: int = 2) -> NoReturn:
    print(f"sshsess: {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------- cleaning ---

# Terminal control noise. We are the terminal emulator here, so anything the
# remote shell emits to paint a prompt has to be stripped before an agent can
# read it -- or before we can find our own sentinels in it.
ANSI_RE = re.compile(
    rb"""
      \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC ... BEL / ST   (window titles)
    | \x1b[P X^_][^\x1b]*\x1b\\           # DCS / SOS / PM / APC
    | \x1b\[[0-?]*[ -/]*[@-~]             # CSI  (colors, cursor moves)
    | \x1b[()#][0-9A-Za-z]                # charset selection
    | \x1b[=>]                            # DECKPAM/DECKPNM (keypad mode)
    | \x1b[@-Z\\-_]                       # two-character escapes
    | [\x00\x07\x0e\x0f]                  # NUL BEL SI SO
    """,
    re.VERBOSE,
)


# Lines that are pure `run` plumbing: the sentinel output itself, and the
# shell's echo of the printf wrapper. They mean nothing to a reader, so they
# are dropped from `read` and from error messages -- but never from clean(),
# which is what `run` searches for those very markers.
PLUMBING_RE = re.compile(r"__SS'{0,2}[BEP][0-9a-f]+__")

# Fingerprints of a login shell we cannot drive. Two distinct symptoms, both
# observed: a parse error on the `{ ... }` wrapper (fish, csh), and fish
# blocking for ~10s on a Primary Device Attribute query because nothing here
# answers terminal queries -- in that case the parse error has not even been
# printed yet when the probe gives up.
NON_POSIX_RE = re.compile(
    r"Unexpected '\}'"
    r"|unopened brace"
    r"|^fish:"
    r"|fish could not read"
    r"|Device Attribute query"
    r"|Badly placed"
    r"|Missing name for redirect",
    re.MULTILINE,
)


def clean(data: bytes) -> str:
    """Turn raw pty bytes into text a human or an agent can read."""
    data = ANSI_RE.sub(b"", data)
    text = data.decode("utf-8", "replace")
    # Collapse *runs* of CR before a newline, not just one. Over a -tt pty,
    # ssh's own one-line diagnostics arrive as "...not known\r\r\n": a single
    # replace() left a trailing \r, and the redraw rule below then kept only
    # what followed it -- nothing. That silently ate every "Could not resolve
    # hostname" / "Connection refused" / "Permission denied" message, which
    # are the ones that actually matter when a session will not open.
    text = re.sub(r"\r+\n", "\n", text)
    out = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        # A bare \r means "redraw this line" (progress bars, spinners).
        # Keep only the final state, which is what a viewer would see.
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        # Collapse backspace-erase sequences emitted by line editors.
        while "\b" in line:
            i = line.index("\b")
            line = line[: max(0, i - 1)] + line[i + 1 :]
        out.append(line)
    return "\n".join(out)


# ----------------------------------------------------------------- daemon ---

def daemon_main(name: str, target: str, ssh_args: list[str], cols: int, rows: int,
                term: str, shell: str = "", harden: bool = True):
    os.umask(0o077)  # long-lived process; do not rely on what it inherited
    d, sock_p, log_p, meta_p = paths(name)

    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    # Anything the daemon itself screams about lands here, not in the void.
    errlog = os.open(str(d / "daemon.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(errlog, 1)
    os.dup2(errlog, 2)

    lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lsock.bind(str(sock_p))
    os.chmod(sock_p, 0o600)  # bind() honours umask, but be explicit: this is a
    lsock.listen(8)          # control channel into a live root-capable shell

    # ssh_args go before the destination: anything after it is taken as the
    # remote command, so `ssh host -p 2222` does not do what you'd hope.
    # A --shell request is exactly that trailing remote command, which is how
    # we escape a non-POSIX login shell like fish.
    argv = ["ssh", "-tt", *SSH_KEEPALIVE, *ssh_args, target]
    if shell:
        argv += shlex.split(shell)
    env = dict(os.environ, TERM=term, LINES=str(rows), COLUMNS=str(cols))

    ssh_pid, master = pty.fork()
    if ssh_pid == 0:
        os.environ.clear()
        os.environ.update(env)
        try:
            os.execvp("ssh", argv)
        except Exception as exc:  # pragma: no cover - child side
            sys.stderr.write(f"exec ssh failed: {exc}\n")
        os._exit(127)

    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    meta = {
        "name": name,
        "target": target,
        "ssh_args": ssh_args,
        "argv": argv,
        "daemon_pid": os.getpid(),
        "ssh_pid": ssh_pid,
        "cols": cols,
        "rows": rows,
        "term": term,
        "shell": shell,
        "harden": harden,
        "started": time.time(),
        "state": "running",
        "owner": os.environ.get("SSHSESS_OWNER", "") or "unknown",
    }
    meta_p.write_text(json.dumps(meta, indent=2))

    log = open(log_p, "ab", buffering=0)
    stopping = {"flag": False}

    def shutdown(_sig=None, _frm=None):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    sel = selectors.DefaultSelector()
    sel.register(master, selectors.EVENT_READ, "pty")
    sel.register(lsock, selectors.EVENT_READ, "sock")

    eof = False
    while not stopping["flag"] and not eof:
        for key, _ in sel.select(timeout=1.0):
            if key.data == "pty":
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    eof = True
                    break
                log.write(chunk)
            else:
                try:
                    conn, _ = lsock.accept()
                except OSError:
                    continue
                if _serve(conn, master, ssh_pid, log_p) == "kill":
                    stopping["flag"] = True

    # Teardown: hang up the remote shell, reap, record why we stopped.
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(ssh_pid, sig)
        except ProcessLookupError:
            break
        for _ in range(20):
            wpid, status = os.waitpid(ssh_pid, os.WNOHANG)
            if wpid:
                meta["ssh_exit"] = (
                    os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
                )
                break
            time.sleep(0.05)
        if "ssh_exit" in meta:
            break

    reason = "ssh exited" if eof else "closed by sshsess kill"
    log.write(f"\n[sshsess] session ended: {reason}\n".encode())
    log.close()
    meta["state"] = "dead"
    meta["ended"] = time.time()
    meta_p.write_text(json.dumps(meta, indent=2))
    try:
        sock_p.unlink()
    except OSError:
        pass
    os._exit(0)


def _serve(conn: socket.socket, master: int, ssh_pid: int, log_p: Path) -> str | None:
    """Handle one request. Returns "kill" if the daemon should stop."""
    action = None
    try:
        conn.settimeout(5.0)
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < 4 << 20:
            part = conn.recv(65536)
            if not part:
                break
            buf += part
        req = json.loads(buf.decode("utf-8", "replace") or "{}")
        op = req.get("op")

        if op == "send":
            data = req.get("data", "").encode("utf-8")
            os.write(master, data)
            reply = {"ok": True, "wrote": len(data), "offset": log_p.stat().st_size}
        elif op == "info":
            alive = True
            try:
                os.kill(ssh_pid, 0)
            except ProcessLookupError:
                alive = False
            reply = {
                "ok": True,
                "ssh_pid": ssh_pid,
                "ssh_alive": alive,
                "bytes": log_p.stat().st_size,
            }
        elif op == "kill":
            action = "kill"
            reply = {"ok": True}
        else:
            reply = {"ok": False, "error": f"unknown op {op!r}"}
        conn.sendall((json.dumps(reply) + "\n").encode())
    except Exception as exc:
        try:
            conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
        except OSError:
            pass
    finally:
        conn.close()
    return action


# ----------------------------------------------------------------- client ---

def request(name: str, req: dict, timeout: float = 10.0) -> dict:
    _, sock_p, _, _ = paths(name)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_p))
    except (FileNotFoundError, ConnectionRefusedError):
        die(f"session {name!r} is not running (no live socket at {sock_p})", 3)
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            part = s.recv(65536)
            if not part:
                break
            buf += part
    finally:
        s.close()
    try:
        return json.loads(buf.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        die("malformed reply from daemon", 3)


def read_log(name: str, offset: int = 0) -> bytes:
    _, _, log_p, _ = paths(name)
    if not log_p.exists():
        return b""
    with open(log_p, "rb") as fh:
        fh.seek(offset)
        return fh.read()


def is_live(name: str) -> bool:
    _, sock_p, _, _ = paths(name)
    if not sock_p.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(str(sock_p))
        return True
    except OSError:
        return False
    finally:
        s.close()


KEYS = {
    "enter": "\r", "return": "\r", "tab": "\t", "escape": "\x1b", "esc": "\x1b",
    "space": " ", "bspace": "\x7f", "backspace": "\x7f", "delete": "\x1b[3~",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F", "pgup": "\x1b[5~", "pgdn": "\x1b[6~",
    # whiptail/dialog menus and installers bind these
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
    "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
    "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}


def key_to_bytes(spec: str) -> str:
    low = spec.lower()
    if low in KEYS:
        return KEYS[low]
    m = re.fullmatch(r"c-([a-z@\[\]\\^_?])", low)
    if m:
        ch = m.group(1)
        return "\x7f" if ch == "?" else chr(ord(ch.upper()) ^ 0x40)
    # A bare printable character is a keypress too, and TUIs live on them:
    # `q` to leave less or top, `y`/`n` in a dialog, `:` in vim. Sending it
    # without a newline is the whole point -- these programs read one byte.
    if len(spec) == 1 and spec.isprintable():
        return spec
    die(f"unknown key {spec!r}; use a single character, C-c, C-d, Enter, Tab, "
        f"Escape, Up, Down, F1-F12, ...")


# ------------------------------------------------------------- run engine ---

def _token() -> str:
    return f"{os.getpid():x}{int(time.time() * 1000) & 0xFFFFFF:x}"


def _sentinel_script(cmd: str, tok: str) -> str:
    """Wrap cmd in a brace group fenced by two sentinels.

    Two tricks make this work against an interactive shell whose prompt we
    know nothing about:

    1. The marker literals are split across a quote boundary ('__SS''B...'),
       so the shell's *echo* of this input does not contain the text we
       search for. Otherwise every search would match the echo first and we
       would slice the output in the wrong place.
    2. Everything is one brace group. The shell cannot execute until it
       reads the closing brace, so all of the input echo (including PS2
       continuation lines) is emitted *before* the opening sentinel's
       output -- which puts it outside the region we extract.

    Keeping cmd on its own physical line, rather than joining with ';', is
    what lets a trailing '&' work: 'foo & ; printf' is a syntax error,
    'foo &' followed by a newline is not.
    """
    # Leading Ctrl-U discards anything already sitting on the input line. A
    # bare `send NAME --key q` leaves a `q` there, and the next wrapper landed
    # on top of it as `q{ printf ...` -> "command not found" plus a syntax
    # error. Ctrl-U is the tty kill character, so it works with or without
    # readline.
    return (
        f"\x15{{ printf '__SS''B{tok}__\\n'\n"
        f"{cmd}\n"
        f"printf '__SS''E{tok}__%s__\\n' \"$?\"; }}\n"
    )


_EXIT_RE = re.compile(r"(?:^|[;&|\n])\s*(exit|logout)\b")


def kills_session(cmd: str) -> str | None:
    """Return the offending word if cmd would exit the session's own shell.

    `run` deliberately executes in the login shell -- that is what preserves
    cwd and exported vars -- so a bare `exit` logs that shell out and takes
    the whole session, and everything accumulated in it, with it. Costly and
    silent, so it is worth catching before it happens.

    Quoted regions and parenthesized subshells are removed first: in
    `sh -c 'exit 1'` and `( exit 42 )` the exit is contained and harmless.
    """
    s = re.sub(r"'[^']*'", " ", cmd)
    s = re.sub(r'"[^"]*"', " ", s)
    for _ in range(4):  # unwind a few levels of nesting
        new = re.sub(r"\([^()]*\)", " ", s)
        if new == s:
            break
        s = new
    m = _EXIT_RE.search(s)
    return m.group(1) if m else None


# --------------------------------------------------------- auto-answering ---
#
# Order here is presentation only: the rule whose match sits *latest* in the
# output wins, because that is the prompt the far side is actually stopped at.
# `confirm` marks an answer that changes something on the far side -- those
# stay off until the caller passes --yes, because a stray "y" is not
# recoverable.
#
# Each rule: label, pattern, and exactly one of send / key / secret.
DEFAULT_RULES = [
    {"label": "sudo-password",
     "pattern": r"\[sudo\] password for [^:]*:|^Password:\s*$|password for .*:\s*$",
     "secret": "sudo"},
    # Only the pager prompts that are actually recognisable as text. A plain
    # `less` shows the filename in reverse video, which is indistinguishable
    # from output once escapes are stripped -- for that, send `q` yourself.
    # The environment hardening applied by `new` means git/systemd/man do not
    # reach for a pager at all, so this rarely comes up.
    # `less -M` renders "lines 1-49/11533 0%", so the pattern has to swallow
    # the rest of the line -- a rule only fires when its match reaches the
    # tail of the output, and that trailing percentage kept it from ever
    # matching.
    {"label": "pager",
     "pattern": r"\(END\)[^\n]*|--More--[^\n]*|lines \d+-\d+/\d+[^\n]*",
     "key": "q"},
    {"label": "press-any-key",
     "pattern": r"[Pp]ress (any key|ENTER|\[Enter\]|RETURN).{0,20}(continue)?",
     "key": "Enter"},
    {"label": "yes-no-confirm",
     # apt -> [Y/n]; dnf -> "Is this ok [y/N]:"; pacman -> "Proceed with
     # installation?"; zypper -> "Continue? [y/n/v/...]", which is why the
     # bracket form allows extra choices inside it.
     "pattern": r"\[Y/n\]|\[y/N\]|\[y/n[^\]]*\]|\(y/n\)|Proceed with installation\?|"
                r"Do you want to continue\?|Is this ok",
     "send": "y", "confirm": True},
    {"label": "dpkg-conffile",
     "pattern": r"What do you want to do about modified configuration file|"
                r"\*\*\* .* \(Y/I/N/O/D/Z\)",
     "send": "N", "confirm": True},
    {"label": "needrestart-services",
     "pattern": r"Which services should be restarted|Daemons using outdated libraries",
     "key": "Enter", "confirm": True},
]

SECRET_REF_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def rules_path_default() -> Path:
    return Path("~/.config/sshsess/rules.json").expanduser()


def secrets_path_default() -> Path:
    return Path("~/.config/sshsess/secrets.json").expanduser()


def load_rules(path: str | None) -> list:
    """Default rules, or the ones in a JSON file ({"rules": [...]})."""
    p = Path(path).expanduser() if path else rules_path_default()
    if not p.exists():
        if path:
            die(f"rules file {p} not found")
        return list(DEFAULT_RULES)
    try:
        blob = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read rules {p}: {exc}")
    # Accept either {"rules": [...], "extend_defaults": true} or a bare list.
    if isinstance(blob, list):
        rules, extend = blob, False
    else:
        rules, extend = blob.get("rules", []), bool(blob.get("extend_defaults"))
    if not isinstance(rules, list) or not rules:
        die(f"{p} contains no rules")
    return list(DEFAULT_RULES) + rules if extend else rules


def load_secrets(path: str | None) -> dict:
    """Secrets live in a 0600 file, never on a command line or in a prompt.

    Refusing a group/world-readable file is not pedantry: everything here ends
    up typed into a remote root shell.
    """
    p = Path(path).expanduser() if path else secrets_path_default()
    if not p.exists():
        if path:
            die(f"secrets file {p} not found")
        return {}
    mode = p.stat().st_mode & 0o077
    if mode:
        die(f"{p} is readable by others (mode {oct(p.stat().st_mode & 0o777)}); "
            f"run: chmod 600 {p}")
    try:
        blob = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read secrets {p}: {exc}")
    return blob.get("secrets", blob) or {}


class Responder:
    """Answers known prompts as they appear, so nothing waits on a human."""

    def __init__(self, rules: list, secrets: dict, allow_confirm: bool, dry_run: bool = False):
        self.allow_confirm = allow_confirm
        self.dry_run = dry_run
        self.secrets = secrets
        self.answered: list[str] = []
        # Kept apart from `answered` on purpose: a missing secret means nothing
        # was sent, and callers count answered lines to assert behaviour.
        self.problems: list[str] = []
        self.rules = []
        for r in rules:
            if r.get("confirm") and not allow_confirm:
                continue
            try:
                rx = re.compile(r["pattern"], re.MULTILINE)
            except (KeyError, re.error) as exc:
                die(f"bad rule {r.get('label', r)!r}: {exc}")
            self.rules.append((r, rx))

    def payload(self, rule: dict):
        """-> (bytes to send, text safe to show) or None."""
        if "secret" in rule:
            name = rule["secret"]
            if not SECRET_REF_RE.match(str(name)):
                die(f"invalid secret name {name!r}")
            if name not in self.secrets:
                return None
            return self.secrets[name] + "\n", f"<secret:{name}>"
        if "key" in rule:
            return key_to_bytes(rule["key"]), f"<key:{rule['key']}>"
        if "send" in rule:
            text = rule["send"]
            return text + ("" if rule.get("no_enter") else "\n"), text
        die(f"rule {rule.get('label')!r} has no send/key/secret")

    def scan(self, name: str, text: str) -> int:
        """Answer a prompt only if the far side is actually waiting at it.

        The load-bearing rule is that the match must sit at the *tail* of what
        has arrived: a prompt is the last thing a program prints before it
        blocks. Matching anywhere in the stream is how a pattern hit the help
        text scrolling through `less` and typed Enter into it three times --
        and Enter into someone else's TUI can confirm a highlighted menu item.
        If output follows the match, the program moved on and there is nothing
        to answer.
        """
        best = None
        for rule, rx in self.rules:
            for m in rx.finditer(text):
                if text[m.end():].strip():
                    continue  # not the tail: this prompt is already history
                if best is None or m.start() > best[1].start():
                    best = (rule, m)
        if not best:
            return 0
        rule, m = best
        got = self.payload(rule)
        if got is None:
            self.problems.append(
                f"{rule['label']}: cannot answer, secret {rule['secret']!r} is not "
                f"configured -- add it to {secrets_path_default()} (mode 0600)"
            )
            return m.end()
        data, shown = got
        if not self.dry_run:
            request(name, {"op": "send", "data": data})
        self.answered.append(f"{rule['label']} -> {shown}")
        # Skip the whole prompt line, not just the matched span. One rule's
        # alternatives can match twice in one line ("Do you want to continue?"
        # and "[Y/n]"), which answered the same question twice. A prompt is a
        # line; once answered, the rest of it is spent. If no newline follows,
        # the prompt is the tail of what has arrived so far, so consume it all.
        nl = text.find("\n", m.end())
        return len(text) if nl < 0 else nl + 1


# Bracketed paste is the useful tell. readline turns it on (`?2004h`) while the
# shell reads a command line and off (`?2004l`) before running anything, so the
# last toggle in the stream says whether the shell is at its prompt -- without
# sending a byte or knowing what the prompt looks like. Measured here: bash at
# a prompt -> h; less, top, whiptail -> l; after quitting less -> h again.
# Alternate-screen switches are checked too: whiptail uses them and they name
# the full-screen case precisely.
BRACKETED_RE = re.compile(rb"\x1b\[\?2004([hl])")
ALTSCREEN_RE = re.compile(rb"\x1b\[\?(?:1049|1047|47)([hl])")


def occupancy(name: str) -> str:
    """"prompt" | "command" | "fullscreen" | "unknown".

    Reads the raw log, so it must run before clean() strips these sequences.
    "unknown" is honest and common: a shell without bracketed paste (dash, or
    readline configured against it) leaves no evidence, and guessing there
    would block work for no reason.
    """
    raw = read_log(name)
    events = []
    for m in BRACKETED_RE.finditer(raw):
        events.append((m.start(), "prompt" if m.group(1) == b"h" else "command"))
    for m in ALTSCREEN_RE.finditer(raw):
        events.append((m.start(), "fullscreen" if m.group(1) == b"h" else "screen-back"))
    if not events:
        return "unknown"
    # Latest evidence by position wins. Checking alternate screen first and
    # returning early meant a TUI killed before it could emit rmcup (SIGKILL on
    # vim, a stray `tput smcup`) left "fullscreen" latched forever, and every
    # later run was refused although bash was demonstrably back at its prompt.
    events.sort()
    last = events[-1][1]
    if last != "screen-back":
        return last
    # The screen was handed back; bracketed paste knows what happened since.
    for _pos, kind in reversed(events):
        if kind in ("prompt", "command"):
            return kind
    return "unknown"


def probe_free(name: str, timeout: float = 2.0) -> bool:
    """Is the remote shell actually reading commands right now?

    The busy flag is a suspicion, not a fact, and a sticky suspicion blocks
    real work: feeding a hung `read` by hand, or answering its prompt with
    `expect`, frees the shell without anything clearing the flag. So verify
    instead of trusting -- send a lone sentinel and see whether it echoes
    back. If a command really is still swallowing input it eats this probe
    too, which is precisely the answer we want.
    """
    tok = _token()
    # Ctrl-U first, same as the run wrapper: a character left on the input line
    # by an earlier `send --key q` otherwise glues itself to the probe
    # ("qprintf: command not found"), the marker never comes back, and a shell
    # that is perfectly free gets reported as busy.
    resp = request(name, {"op": "send", "data": f"\x15printf '__SS''P{tok}__\\n'\n"})
    start = int(resp.get("offset", 0))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if f"__SSP{tok}__" in clean(read_log(name, start)):
            return True
        time.sleep(POLL_FAST)
    return False


def _strip_plumbing(text: str, tok: str) -> str:
    """Best-effort body for a run that never reached its closing sentinel.

    Timed-out and interrupted commands still carry useful output, but it is
    surrounded by wrapper echo, so drop everything up to the opening sentinel
    and any leftover marker lines -- otherwise `__SSE...` strings show up in
    what looks like ordinary command output.
    """
    if f"__SSB{tok}__" in text:
        text = text.split(f"__SSB{tok}__", 1)[1].lstrip("\n")
    keep = [l for l in text.split("\n") if not PLUMBING_RE.search(l)]
    return "\n".join(keep)


def _extract(text: str, tok: str):
    """-> (body, exit_code) once both markers are present, else None."""
    beg = f"__SSB{tok}__"
    i = text.find(beg)
    if i < 0:
        return None
    m = re.compile(re.escape(f"__SSE{tok}__") + r"(\d+)__").search(text, i)
    if not m:
        return None
    body = text[i + len(beg) : m.start()]
    if body.startswith("\n"):
        body = body[1:]
    return body, int(m.group(1))


def do_run(name: str, cmd: str, timeout: float, responder: "Responder | None" = None,
           force: bool = False):
    """Run cmd in the live remote shell; return (body, exit_code, timed_out).

    The timeout is one budget covering both waiting for the session to free up
    and the command itself, so a caller never waits appreciably longer than it
    asked for.
    """
    deadline = time.time() + timeout
    # Check liveness first: SessionLock opens a file inside the session dir,
    # which for a name that was never created does not exist, and the caller
    # got a FileNotFoundError traceback instead of a clean "not running".
    if not is_live(name):
        _, sock_p, _, _ = paths(name)
        die(f"session {name!r} is not running (no live socket at {sock_p})", 3)
    try:
        with SessionLock(name, deadline):
            _preflight(name, force)
            return _do_run_locked(name, cmd, deadline, responder)
    except Busy:
        die(
            f"session {name!r} is busy: another command is still holding it "
            f"(waited {timeout:g}s). Use a separate session for parallel work, "
            f"or `read {name} --tail 20` to see what it is doing.",
            125,
        )


def _preflight(name: str, force: bool):
    """Refuse to type into something that is not a shell waiting for commands.

    Runs *inside* the lock. Outside it, a second concurrent `run` would see the
    first one's command executing, conclude the shell is occupied and fail with
    126 -- instead of queueing, which is the whole point of the lock.
    """
    reason = busy_reason(name)
    state = occupancy(name)
    if state == "prompt":
        # A command's own output can contain `ESC[?2004h` -- a nested REPL emits
        # it legitimately -- so the log alone is not proof enough to overrule a
        # standing busy flag. Costs a round-trip, but only in that rare case.
        if reason and not force:
            if probe_free(name):
                clear_busy(name)
            else:
                die(
                    f"session {name!r} looks idle in the log, but a probe sent to it "
                    f"was swallowed -- something is still reading input there.\n"
                    f"  reason: {reason}\n"
                    f"  look:      sshsess read {name} --tail 20\n"
                    f"  Ctrl-C it: sshsess interrupt {name}\n"
                    f"  override:  run --force",
                    126,
                )
    elif state in ("fullscreen", "command") and not force:
        if state == "fullscreen":
            what = ("a full-screen program owns the terminal (it switched to the "
                    "alternate screen)")
            fix = (f"  quit it with its own key:\n"
                   f"    sshsess send {name} --key q      # less, top, man\n"
                   f"    sshsess send {name} --key Enter  # whiptail/dialog\n"
                   f"    sshsess send {name} --key C-x    # nano")
        else:
            # Bracketed paste cannot tell a long-running command from `less`,
            # which also holds the terminal but ignores SIGINT -- so offer both
            # exits instead of confidently recommending the wrong one.
            what = "the shell is not at a prompt -- something is still running there"
            fix = (f"  see what it is:  sshsess read {name} --tail 20\n"
                   f"  a command?       sshsess interrupt {name}\n"
                   f"  a pager or TUI?  it ignores Ctrl-C -- send its own key, e.g. "
                   f"`sshsess send {name} --key q`")
        die(
            f"refusing to run in {name!r}: {what}. Sent now, the wrapper would be "
            f"read as keystrokes, not as a command."
            + (f"\n  last known cause: {reason}" if reason else "")
            + f"\n{fix}\n  override: run --force",
            126,
        )
    elif state == "unknown" and reason and not force:
        # No bracketed-paste evidence, so fall back to asking the shell.
        if probe_free(name):
            clear_busy(name)
        else:
            die(
                f"session {name!r} still has a command running -- verified: it "
                f"swallowed a probe. Anything sent now would be eaten by it, which "
                f"is what produces corrupted output and `syntax error near "
                f"unexpected token '}}'`.\n"
                f"  reason: {reason}\n"
                f"  look:      sshsess read {name} --tail 20\n"
                f"  Ctrl-C it: sshsess interrupt {name}\n"
                f"  full-screen program? send its own quit key, e.g. "
                f"`sshsess send {name} --key q`\n"
                f"  override:  run --force  (returns 124 after the full --timeout "
                f"if the command really is stuck)",
                126,
            )


def _do_run_locked(name: str, cmd: str, deadline: float, responder: "Responder | None" = None):
    # Drop a cancel flag left by an `interrupt` that had no run to unblock
    # (e.g. issued after a timeout). Otherwise the *next* run reads it and
    # returns 130 before the command has had a chance to do anything.
    try:
        (sdir(name) / "cancel").unlink()
    except OSError:
        pass
    tok = _token()
    resp = request(name, {"op": "send", "data": _sentinel_script(cmd, tok)})
    if not resp.get("ok"):
        die(f"send failed: {resp.get('error')}", 3)
    # The daemon stats the log after writing to the pty, so our command's
    # output is always past this offset. Tokens are unique, so scanning from
    # exactly here cannot match an older command's markers.
    start = int(resp.get("offset", 0))

    cancel_p = sdir(name) / "cancel"
    delay = POLL_FAST
    seen = 0  # how much of the body the responder has already looked at
    while True:
        text = clean(read_log(name, start))
        got = _extract(text, tok)
        if got:
            clear_busy(name)
            return got[0], got[1], False
        # Answer prompts while the command is still running. Scanning only the
        # unexamined part -- and advancing past each match -- keeps us from
        # replying twice to one prompt, including to its own echo.
        if responder is not None:
            while True:
                used = responder.scan(name, text[seen:])
                if not used:
                    break
                seen += used
                delay = POLL_FAST
        # Ctrl-C aborts the whole brace group, so the closing sentinel never
        # runs and this loop would otherwise wait out the full timeout while
        # the remote shell sits idle at a prompt -- holding the lock and
        # handing spurious "busy" errors to everyone else. `interrupt` drops
        # this flag so we can stop waiting immediately.
        if cancel_p.exists():
            try:
                cancel_p.unlink()
            except OSError:
                pass
            # Ctrl-C returned the shell to its prompt, so it is usable again.
            clear_busy(name)
            return _strip_plumbing(text, tok), 130, False
        if time.time() >= deadline:
            # Hand back whatever arrived; a hung command is still evidence.
            mark_busy(name, f"a command timed out and is still running: {cmd[:120]}")
            return _strip_plumbing(text, tok), 124, True
        if not is_live(name):
            die(
                f"session {name!r} died while running the command "
                f"(a command that exits the shell will do this). Last output:\n"
                f"{'-' * 60}\n{error_tail(name)}\n{'-' * 60}",
                3,
            )
        time.sleep(delay)
        delay = min(POLL_SLOW, delay * 1.5)


# ------------------------------------------------------------- subcommands ---

def cmd_new(a):
    d, sock_p, log_p, meta_p = paths(a.name)
    if is_live(a.name):
        if not a.force:
            who = ""
            try:
                other = json.loads(meta_p.read_text()).get("owner", "")
                if other and other != owner_id():
                    who = f"\nIt was opened by a different owner ({other[:8]}...)."
                elif other:
                    # Deliberately not "so just use it": ownership is only
                    # tracked per chat, so a parallel subagent of this same
                    # chat is indistinguishable from us and may be mid-deploy
                    # in that shell.
                    who = (
                        f"\nIt has this chat's owner id, but that cannot tell you "
                        f"whether it is yours or a parallel agent's -- subagents "
                        f"share one id."
                    )
            except (OSError, json.JSONDecodeError):
                pass
            die(f"session {a.name!r} is already running.{who}\n"
                f"Safest: pick a different, task-specific name. "
                f"`ls` shows what exists; --force replaces it; `kill` closes it.")
        cmd_kill(argparse.Namespace(name=a.name, all=False, quiet=True))
        time.sleep(0.3)

    # AF_UNIX paths are capped by the kernel -- 104 bytes on macOS/BSD, 108 on
    # Linux. Over the limit, bind() fails inside the daemon and the client only
    # reports "daemon failed to start", so say the real thing up front.
    if len(str(sock_p).encode()) >= 104:
        die(f"the control socket path is too long for AF_UNIX "
            f"({len(str(sock_p).encode())} bytes, limit ~104):\n  {sock_p}\n"
            f"Use a shorter session name, or point SSHSESS_DIR somewhere short "
            f"(e.g. SSHSESS_DIR=/tmp/ss).")

    d.mkdir(parents=True, exist_ok=True)
    # mkdir(exist_ok=True) leaves an existing directory's mode alone, so a dir
    # created by an older version (or a pre-existing SSHSESS_DIR) stays loose
    # until it is tightened explicitly.
    for p in (state_root(), d):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    # `busy` and `cancel` describe the *previous* shell. Left behind, a stale
    # busy flag made `new` itself fail with 126 while the session came up
    # live-but-unhardened, and `reconnect` inherited the same trap.
    for p in (sock_p, log_p, meta_p, d / "daemon.log", d / "busy", d / "cancel"):
        try:
            p.unlink()
        except OSError:
            pass

    # The daemon inherits this, so meta.json records which chat opened the
    # session and `ls` can show it.
    os.environ["SSHSESS_OWNER"] = owner_id()

    pid = os.fork()
    if pid == 0:
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
        daemon_main(a.name, a.target, a.ssh_args, a.cols, a.rows, a.term, a.shell,
                    not a.no_harden)
        os._exit(0)
    os.waitpid(pid, 0)

    for _ in range(100):
        if is_live(a.name):
            break
        time.sleep(0.05)
    else:
        # Usually this is not the daemon failing -- it is ssh dying instantly
        # (bad option, refused connection, DNS) and the daemon tidying up
        # after it. The diagnosis is whatever ssh printed, so lead with that.
        tail = error_tail(a.name, 15)
        detail = ""
        try:
            meta = json.loads(meta_p.read_text())
            if meta.get("ssh_exit") is not None:
                detail = f"\nssh exited with status {meta['ssh_exit']}; it was run as:\n  {' '.join(meta['argv'])}"
        except (OSError, json.JSONDecodeError):
            pass
        if tail or detail:
            die(f"session {a.name!r} could not be opened.{detail}\n"
                f"{'-' * 60}\n{tail[-2000:]}\n{'-' * 60}", 3)
        die(f"daemon failed to start; see {d / 'daemon.log'}", 3)

    # Readiness probe: a shell that can echo a sentinel back is a shell we can
    # drive. If this times out the tail of out.log is the real diagnosis --
    # host-key prompt, password prompt, permission denied, DNS failure.
    _body, _code, timed_out = do_run(a.name, "true", a.timeout)
    if timed_out:
        tail = error_tail(a.name, 15)
        # Two very different causes produce this one symptom. Scan the whole
        # log, not just the displayed tail: a chatty login shell can push the
        # telltale line out of the window. When the evidence is unclear, show
        # both causes rather than guessing confidently and wrongly.
        args_suffix = f" -- {' '.join(a.ssh_args)}" if a.ssh_args else ""
        shell_fix = (
            f"  sshsess new {a.name} {a.target} --shell 'bash -i'{args_suffix}"
        )
        prompt_fix = (
            f"  sshsess send {a.name} yes        # then: sshsess read {a.name}"
        )
        if NON_POSIX_RE.search(clean(read_log(a.name))):
            hint = (
                f"The remote login shell cannot be driven: it either failed to parse "
                f"the wrapper or stalled on a terminal query (fish and csh do both).\n"
                f"Retry with an explicit POSIX shell:\n{shell_fix}"
            )
        else:
            hint = (
                f"Two things usually cause this:\n"
                f"  1. the connection is waiting on input (host key / password):\n"
                f"{prompt_fix}\n"
                f"  2. the login shell is not POSIX (fish, csh) or is very slow:\n"
                f"{shell_fix}\n"
                f"Give up with: sshsess kill {a.name}"
            )
        print(
            f"sshsess: session {a.name!r} started but the remote shell did not respond "
            f"within {a.timeout:g}s. Last output:\n"
            f"{'-' * 60}\n{tail}\n{'-' * 60}\n{hint}",
            file=sys.stderr,
        )
        raise SystemExit(4)

    note = ""
    if not a.no_harden:
        _, code, _ = do_run(a.name, HARDEN_SCRIPT, 20.0, force=True)
        note = " (non-interactive env applied)" if code == 0 else " (env hardening failed)"
    print(f"session {a.name!r} ready -> {a.target}{note}")


def cmd_send(a):
    text = " ".join(a.text)
    if a.key:
        payload = "".join(key_to_bytes(k) for k in a.key)
        if text:
            payload = text + payload
    elif a.paste:
        # Modern REPLs (Python 3.13+ PyREPL) auto-indent every continuation
        # line, so typed-in indentation is added to theirs and a pasted block
        # collapses into an IndentationError. Bracketed paste tells the reader
        # to take the text verbatim.
        payload = "\x1b[200~" + text + "\x1b[201~" + ("" if a.no_enter else "\r")
    else:
        payload = text + ("" if a.no_enter else "\n")
    resp = request(a.name, {"op": "send", "data": payload})
    if not resp.get("ok"):
        die(f"send failed: {resp.get('error')}", 3)
    if a.wait:
        time.sleep(a.wait)
        # Exactly what arrived after our write -- not a byte before it, or
        # every session since boot scrolls past.
        text = clean(read_log(a.name, resp["offset"]))
        sys.stdout.write(text)
        if not text.endswith("\n"):
            print()


def cmd_expect(a):
    """Answer prompts in a session nobody is watching.

    Use this when a session is already blocked, when driving a TUI, or to
    babysit something started with `send`. `run` does this inline already.
    """
    responder = Responder(
        load_rules(a.rules), load_secrets(a.secrets),
        allow_confirm=a.yes, dry_run=a.dry_run,
    )
    if a.list_rules:
        # Show every rule, including the ones gated behind --yes: someone
        # checking "is there a [Y/n] rule?" must not conclude there is none.
        every = Responder(load_rules(a.rules), {}, allow_confirm=True)
        for rule, _ in every.rules:
            what = rule.get("send") or rule.get("key") or f"<secret:{rule.get('secret')}>"
            gate = "  [needs --yes]" if rule.get("confirm") else ""
            print(f"{rule['label']:<24} -> {what!r:<12} /{rule['pattern']}/{gate}")
        return
    if not is_live(a.name):
        die(f"session {a.name!r} is not running", 3)

    _, _, log_p, _ = paths(a.name)
    if a.since is not None:
        start = a.since
    else:
        # Look back, not just forward: the usual reason to run `expect` is that
        # a session is *already* sitting at a prompt printed before we started.
        # This is safe only because a rule fires solely when its match is at
        # the tail of the output, so older prompts in the lookback are inert.
        size = log_p.stat().st_size if log_p.exists() else 0
        start = max(0, size - EXPECT_LOOKBACK)
    seen = 0
    deadline = time.time() + a.timeout
    delay = POLL_FAST
    while time.time() < deadline:
        text = clean(read_log(a.name, start))
        used = responder.scan(a.name, text[seen:])
        if used:
            seen += used
            delay = POLL_FAST
            if a.once:
                break
            continue
        if not is_live(a.name):
            break
        time.sleep(delay)
        delay = min(POLL_SLOW, delay * 1.5)

    verb = "would answer" if a.dry_run else "answered"
    for line in responder.problems:
        print(f"NOT answered -- {line}", file=sys.stderr)
    if responder.answered:
        for line in responder.answered:
            print(f"{verb} {line}")
    elif responder.problems:
        raise SystemExit(1)
    else:
        print("nothing matched")
        raise SystemExit(1 if a.once else 0)


def cmd_interrupt(a):
    """Ctrl-C the remote command and release a `run` that is waiting on it."""
    if not is_live(a.name):
        die(f"session {a.name!r} is not running", 3)
    (sdir(a.name) / "cancel").write_text("")
    resp = request(a.name, {"op": "send", "data": "\x03"})
    if not resp.get("ok"):
        die(f"interrupt failed: {resp.get('error')}", 3)
    # Do not just declare success: full-screen programs ignore SIGINT. `less`
    # in particular stays put, and clearing the flag here used to let the next
    # `run` type its wrapper into it as keystrokes.
    if probe_free(a.name, 3.0):
        clear_busy(a.name)
        print(f"interrupted {a.name!r}; shell is back at its prompt")
        return
    print(
        f"sshsess: sent Ctrl-C to {a.name!r}, but the shell is still not reading "
        f"commands -- something is ignoring SIGINT.\n"
        f"  If a full-screen program is running, send its own quit key:\n"
        f"    sshsess send {a.name} --key q      # less, top, man\n"
        f"    sshsess send {a.name} --key C-x    # nano\n"
        f"  Look first: sshsess read {a.name} --tail 20",
        file=sys.stderr,
    )
    raise SystemExit(126)


def cmd_read(a):
    data = read_log(a.name, a.since)
    if a.raw:
        sys.stdout.buffer.write(data)
        return
    text = clean(data)
    if not a.no_filter:
        text = "\n".join(l for l in text.split("\n") if not PLUMBING_RE.search(l))
    if a.tail and not a.all:
        text = "\n".join(text.split("\n")[-a.tail :])
    sys.stdout.write(text)
    if not text.endswith("\n"):
        print()
    if a.offset:
        _, _, log_p, _ = paths(a.name)
        size = log_p.stat().st_size if log_p.exists() else 0
        print(f"[sshsess offset={size}]", file=sys.stderr)


def cmd_run(a):
    if not a.cmd:
        die("run needs a command")
    cmd = " ".join(a.cmd)
    responder = None
    if not a.no_auto:
        responder = Responder(
            load_rules(a.rules), load_secrets(a.secrets), allow_confirm=a.yes
        )
    if responder and a.force and occupancy(a.name) in ("fullscreen", "command"):
        # --force overrides the guard, not common sense: with a TUI on screen the
        # rules match its painted text and start pressing keys inside it (three
        # Enters into a live `less`, observed).
        print(f"sshsess: {a.name!r} is not at a prompt -- auto-answering disabled "
              f"for this --force run", file=sys.stderr)
        responder = None
    if a.allow_exit and kills_session(cmd):
        # Asked to close the session on purpose: do it without the alarming
        # "died while running the command" post-mortem.
        request(a.name, {"op": "send", "data": cmd + "\n"})
        for _ in range(60):
            if not is_live(a.name):
                break
            time.sleep(0.1)
        print(f"session {a.name!r} closed by request")
        return
    word = None if a.allow_exit else kills_session(cmd)
    if word:
        die(
            f"refusing to run this: `{word}` would log out the session's own shell "
            f"and destroy it along with its cwd, exported vars and background jobs.\n"
            f"  Contain it in a subshell:  ( {cmd} )\n"
            f"  or run it separately:      sh -c {shlex.quote(cmd)}\n"
            f"  If closing the session is what you want: `kill {a.name}`, "
            f"or pass --allow-exit."
        )
    body, code, timed_out = do_run(a.name, cmd, a.timeout, responder, force=a.force)
    sys.stdout.write(body)
    if body and not body.endswith("\n"):
        print()
    if responder:
        for line in responder.answered:
            print(f"sshsess: auto-answered {line}", file=sys.stderr)
        for line in responder.problems:
            print(f"sshsess: PROMPT NOT ANSWERED -- {line}", file=sys.stderr)
    if timed_out:
        print(
            f"sshsess: command still running after {a.timeout:g}s "
            f"(output above is partial; the remote shell is now busy -- "
            f"use `read`/`send` to deal with it)",
            file=sys.stderr,
        )
    raise SystemExit(code)


def cmd_wait(a):
    rx = re.compile(a.pattern)
    _, _, log_p, _ = paths(a.name)
    if a.since is not None:
        start = a.since
    elif a.from_start:
        start = 0
    else:
        # "wait" means wait for something to happen, so only future output
        # counts. Scanning from byte 0 would match this session's scrollback
        # and return instantly.
        start = log_p.stat().st_size if log_p.exists() else 0
    deadline = time.time() + a.timeout
    delay = POLL_FAST
    while True:
        text = clean(read_log(a.name, start))
        m = rx.search(text)
        if m:
            print(m.group(0))
            return
        if time.time() >= deadline:
            print(
                f"sshsess: pattern {a.pattern!r} did not appear within {a.timeout:g}s",
                file=sys.stderr,
            )
            raise SystemExit(124)
        if not is_live(a.name):
            die(f"session {a.name!r} is no longer running", 3)
        time.sleep(delay)
        delay = min(POLL_SLOW, delay * 1.5)


def cmd_ls(a):
    root = state_root()
    rows = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            meta = {}
            try:
                meta = json.loads((d / "meta.json").read_text())
            except (OSError, json.JSONDecodeError):
                pass
            live = is_live(d.name)
            size = (d / "out.log").stat().st_size if (d / "out.log").exists() else 0
            own = meta.get("owner", "unknown")
            # "same owner", not "mine": parallel subagents share one id.
            tag = "same owner" if own == owner_id() else f"other:{own[:8]}"
            state = "live" if live else "dead"
            if live and busy_reason(d.name):
                state = "busy"  # a command is still occupying the shell
            rows.append((d.name, state, meta.get("target", "?"), tag, size))
    if not rows:
        print("no sessions")
        # Sessions from before ~/.cache stopped being the default would be
        # invisible otherwise, which reads as "my session vanished".
        if root != LEGACY_ROOT and LEGACY_ROOT.is_dir() and any(LEGACY_ROOT.iterdir()):
            print(f"note: an older state directory exists at {LEGACY_ROOT}; "
                  f"inspect it with SSHSESS_DIR={LEGACY_ROOT} sshsess ls")
        return
    w = max(len(r[0]) for r in rows)
    t = max(len(r[2]) for r in rows)
    for name, state, target, tag, size in rows:
        print(f"{name:<{w}}  {state:<4}  {target:<{t}}  {tag:<14}  {size} bytes logged")
    if any(r[1] == "busy" for r in rows):
        print("\nbusy = a command is still running there; `interrupt <name>` frees it")


def cmd_kill(a):
    names = []
    if a.all:
        root = state_root()
        if root.is_dir():
            names = [d.name for d in root.iterdir() if d.is_dir()]
    else:
        names = [a.name]
    for name in names:
        d, _, _, meta_p = paths(name)
        if not d.exists():
            # Reporting "killed" for a name that never existed is a lie that
            # hides typos.
            print(f"no such session {name!r}", file=sys.stderr)
            raise SystemExit(3)
        if not is_live(name):
            if not getattr(a, "quiet", False):
                print(f"{name!r} was already dead")
            continue
        if is_live(name):
            try:
                request(name, {"op": "kill"}, timeout=5.0)
            except SystemExit:
                pass
        else:
            # No socket: fall back to the recorded pids.
            try:
                meta = json.loads(meta_p.read_text())
                for k in ("ssh_pid", "daemon_pid"):
                    if meta.get(k):
                        try:
                            os.kill(meta[k], signal.SIGTERM)
                        except ProcessLookupError:
                            pass
            except (OSError, json.JSONDecodeError):
                pass
        for _ in range(40):
            if not is_live(name):
                break
            time.sleep(0.05)
        if not getattr(a, "quiet", False):
            print(f"killed {name!r}")


def cmd_reconnect(a):
    """Reopen a session with the parameters it was created with.

    After a dropped link the ssh flags, --shell and pty size are all recorded
    in meta.json, so nothing needs to remember them. The remote *state* is
    genuinely gone though -- new shell, new cwd, no exported vars -- so say so
    rather than letting the caller assume otherwise.
    """
    _, _, _, meta_p = paths(a.name)
    try:
        meta = json.loads(meta_p.read_text())
    except (OSError, json.JSONDecodeError):
        die(f"no recorded parameters for {a.name!r}; open it with `new`", 3)

    ns = argparse.Namespace(
        name=a.name, target=meta["target"], ssh_args=meta.get("ssh_args", []),
        cols=meta.get("cols", DEFAULT_COLS), rows=meta.get("rows", DEFAULT_ROWS),
        term=meta.get("term", "xterm-256color"), shell=meta.get("shell", ""),
        timeout=a.timeout, force=True,
        no_harden=not meta.get("harden", True),
    )
    print(f"reconnecting {a.name!r} -> {ns.target}"
          + (f" --shell {ns.shell!r}" if ns.shell else ""))
    cmd_new(ns)
    print("note: this is a fresh shell -- cwd, exported vars and background jobs are gone")


def cmd_truncate(a):
    """Empty a live session's log without touching the connection.

    out.log is append-only and never rotates, so a session that ran a TUI for
    a few minutes carries megabytes of screen repaints. Refuses while a run
    holds the lock, because that run tracks its output by byte offset.
    """
    if not is_live(a.name):
        die(f"session {a.name!r} is not running", 3)
    # The log is also the only evidence of whether the shell is at a prompt.
    # Wiping it while a TUI is up left `run` with nothing to go on, and the
    # wrapper went into the TUI as keystrokes again.
    state = occupancy(a.name)
    if state in ("fullscreen", "command"):
        die(f"refusing to truncate {a.name!r}: the shell is not at a prompt "
            f"({state}), and the log is what tells `run` that. Free the session "
            f"first (`read {a.name} --tail 20`, then `interrupt` or its quit key).",
            126)
    _, _, log_p, _ = paths(a.name)
    lock = SessionLock(a.name, time.time())  # deadline now => never waits
    try:
        with lock:
            before = log_p.stat().st_size if log_p.exists() else 0
            with open(log_p, "r+b") as fh:
                fh.truncate(0)
            print(f"truncated {a.name!r}: {before} bytes dropped")
    except Busy:
        die(f"session {a.name!r} is executing a command right now; "
            f"truncating would confuse it. Try again when it finishes.", 125)


def cmd_prune(_a):
    """Drop dead sessions. Kept manual: their logs are the post-mortem."""
    root = state_root()
    gone = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and not is_live(d.name):
                for f in d.iterdir():
                    try:
                        f.unlink()
                    except OSError:
                        pass
                try:
                    d.rmdir()
                    gone.append(d.name)
                except OSError:
                    pass
    print(f"pruned {len(gone)}: {', '.join(gone)}" if gone else "nothing to prune")


def cmd_info(a):
    if not is_live(a.name):
        # A dead session is exactly when you need its log, so print where it
        # is instead of making the caller reconstruct the path by hand.
        d, _, log_p, meta_p = paths(a.name)
        print(f"session   {a.name}")
        print("state     dead")
        if log_p.exists():
            print(f"log       {log_p}  ({log_p.stat().st_size} bytes)")
        try:
            meta = json.loads(meta_p.read_text())
            print(f"target    {meta.get('target', '?')}")
            print(f"owner     {meta.get('owner', 'unknown')}")
            if meta.get("ssh_exit") is not None:
                print(f"ssh_exit  {meta['ssh_exit']}")
        except (OSError, json.JSONDecodeError):
            print(f"(no metadata in {d})")
        tail = error_tail(a.name, 8)
        if tail:
            print(f"last output:\n{tail}")
        raise SystemExit(3)
    resp = request(a.name, {"op": "info"})
    _, _, log_p, meta_p = paths(a.name)
    meta = json.loads(meta_p.read_text())
    print(f"session   {a.name}")
    print(f"target    {meta['target']}")
    print(f"state     live (ssh pid {resp['ssh_pid']}, remote alive={resp['ssh_alive']})")
    own = meta.get("owner", "unknown")
    print(f"owner     {own}{'  (this chat)' if own == owner_id() else ''}")
    print(f"pty       {meta['cols']}x{meta['rows']}  TERM={meta['term']}")
    print(f"uptime    {time.time() - meta['started']:.0f}s")
    if meta.get("shell"):
        print(f"shell     {meta['shell']}")
    reason = busy_reason(a.name)
    if reason:
        print(f"busy      {reason}\n          -> `sshsess interrupt {a.name}` to free it")
    print(f"log       {log_p}  ({resp['bytes']} bytes)")


# -------------------------------------------------------------------- cli ---

def build_parser():
    p = argparse.ArgumentParser(
        prog="sshsess",
        description="Persistent SSH sessions: one connection, many commands, state preserved.",
    )
    sub = p.add_subparsers(dest="op", required=True)

    n = sub.add_parser("new", help="open a named persistent session")
    n.add_argument("name")
    n.add_argument("target", help="ssh destination, e.g. user@host or a Host from ~/.ssh/config")
    n.add_argument("ssh_args", nargs="*", help="extra ssh args (put them after --)")
    n.add_argument("--cols", type=int, default=DEFAULT_COLS)
    n.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    n.add_argument("--term", default="xterm-256color")
    n.add_argument("--shell", default="",
                   help="run this instead of the login shell, e.g. --shell 'bash -i'. "
                        "Needed when the remote login shell is not POSIX (fish, csh).")
    n.add_argument("--timeout", type=float, default=CONNECT_TIMEOUT,
                   help="seconds to wait for the remote shell to respond")
    n.add_argument("--force", action="store_true", help="replace an existing live session")
    n.add_argument("--no-harden", action="store_true",
                   help="do not export the non-interactive environment (pagers, "
                        "DEBIAN_FRONTEND, EDITOR, LC_ALL) after connecting")
    n.set_defaults(fn=cmd_new)

    r = sub.add_parser("run", help="run a command in the live shell, return output + exit code")
    r.add_argument("--timeout", type=float, default=120.0)
    r.add_argument("--allow-exit", action="store_true",
                   help="permit a command that exits (and thus closes) the session")
    r.add_argument("--yes", action="store_true",
                   help="also auto-answer confirmation prompts ([Y/n], Proceed?, ...)")
    r.add_argument("--no-auto", action="store_true",
                   help="do not auto-answer anything")
    r.add_argument("--force", action="store_true",
                   help="run even though a previous command is still occupying the shell")
    r.add_argument("--rules", metavar="FILE", help="rules file (default ~/.config/sshsess/rules.json)")
    r.add_argument("--secrets", metavar="FILE",
                   help="secrets file (default ~/.config/sshsess/secrets.json, must be 0600)")
    r.add_argument("name")
    # REMAINDER so the remote command keeps its own flags: `run box ls -la`
    # must not have -la parsed as an sshsess option. Consequence: --timeout
    # has to come before the session name.
    r.add_argument("cmd", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("send", help="send raw input (for prompts, TUIs, REPLs)")
    s.add_argument("name")
    s.add_argument("text", nargs="*")
    s.add_argument("--key", action="append", metavar="KEY",
                   help="special key: C-c, C-d, Enter, Tab, Escape, Up, ... (repeatable)")
    s.add_argument("--no-enter", action="store_true", help="do not append a newline")
    s.add_argument("--wait", type=float, metavar="SEC",
                   help="after sending, sleep SEC then print what arrived")
    s.add_argument("--paste", action="store_true",
                   help="wrap in bracketed paste; needed for multi-line input to "
                        "REPLs that auto-indent (Python 3.13+)")
    s.set_defaults(fn=cmd_send)

    e = sub.add_parser("expect", help="watch a session and answer prompts as they appear")
    e.add_argument("name", nargs="?")
    e.add_argument("--timeout", type=float, default=120.0)
    e.add_argument("--once", action="store_true", help="stop after the first answer")
    e.add_argument("--yes", action="store_true", help="also answer confirmation prompts")
    e.add_argument("--dry-run", action="store_true", help="report matches, send nothing")
    e.add_argument("--list-rules", action="store_true", help="print effective rules and exit")
    e.add_argument("--since", type=int, default=None, metavar="OFFSET")
    e.add_argument("--rules", metavar="FILE")
    e.add_argument("--secrets", metavar="FILE")
    e.set_defaults(fn=cmd_expect)

    it = sub.add_parser("interrupt", help="Ctrl-C the remote command and unblock a waiting run")
    it.add_argument("name")
    it.set_defaults(fn=cmd_interrupt)

    d = sub.add_parser("read", help="read what the session has printed")
    d.add_argument("name")
    d.add_argument("--tail", type=int, default=100, help="last N lines (default 100)")
    d.add_argument("--all", action="store_true", help="whole log")
    d.add_argument("--since", type=int, default=0, metavar="OFFSET", help="start at byte OFFSET")
    d.add_argument("--raw", action="store_true", help="raw pty bytes, escapes intact")
    d.add_argument("--no-filter", action="store_true",
                   help="keep the run sentinel lines that are normally hidden")
    d.add_argument("--offset", action="store_true", help="print current end offset on stderr")
    d.set_defaults(fn=cmd_read)

    w = sub.add_parser("wait", help="block until output matches a regex")
    w.add_argument("name")
    w.add_argument("pattern")
    w.add_argument("--timeout", type=float, default=60.0)
    w.add_argument("--since", type=int, default=None, metavar="OFFSET",
                   help="scan from byte OFFSET (get one from `read --offset`)")
    w.add_argument("--from-start", action="store_true",
                   help="also scan existing scrollback, not just new output")
    w.set_defaults(fn=cmd_wait)

    i = sub.add_parser("info", help="status of one session")
    i.add_argument("name")
    i.set_defaults(fn=cmd_info)

    l = sub.add_parser("ls", help="list sessions")
    l.set_defaults(fn=cmd_ls)

    k = sub.add_parser("kill", help="close a session")
    k.add_argument("name", nargs="?")
    k.add_argument("--all", action="store_true")
    k.set_defaults(fn=cmd_kill)

    rc = sub.add_parser("reconnect", help="reopen a session with its recorded ssh args")
    rc.add_argument("name")
    rc.add_argument("--timeout", type=float, default=CONNECT_TIMEOUT)
    rc.set_defaults(fn=cmd_reconnect)

    tr = sub.add_parser("truncate", help="empty a live session's log, keep the connection")
    tr.add_argument("name")
    tr.set_defaults(fn=cmd_truncate)

    pr = sub.add_parser("prune", help="forget dead sessions (drops their logs)")
    pr.set_defaults(fn=cmd_prune)

    return p


def main(argv=None):
    # Everything we create lives under the session dir: the output log, the
    # metadata, the control socket. The log can hold whatever the remote shell
    # printed -- including a password, if a badly written prompt forgot to turn
    # echo off -- so nothing here should be group- or world-readable. On Linux
    # $XDG_RUNTIME_DIR happens to be 0700 and hid this; on macOS there is no
    # such directory and the fallback is ~/.cache, where it would not be hidden.
    os.umask(0o077)
    argv = list(sys.argv[1:] if argv is None else argv)
    # Pull `-- ssh args` off by hand for `new`. argparse before 3.12 cannot
    # match a nargs="*" positional after `--` once any optional flag preceded
    # it: on 3.8-3.11 `new x host --shell 'bash -i' -- -F cfg` died with
    # "unrecognized arguments". Splitting here makes the CLI behave the same
    # on every supported interpreter. Only `new` is touched, because a `run`
    # command line may legitimately contain its own `--`.
    tail = []
    if argv and argv[0] == "new" and "--" in argv:
        cut = argv.index("--")
        tail = argv[cut + 1 :]
        argv = argv[:cut]

    a = build_parser().parse_args(argv)
    if tail:
        a.ssh_args = tail
    if a.op == "kill" and not a.all and not a.name:
        die("kill needs a session name, or --all")
    a.fn(a)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        os._exit(0)
    except KeyboardInterrupt:
        raise SystemExit(130)
