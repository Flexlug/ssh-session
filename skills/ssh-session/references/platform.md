# Platform and requirements

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

