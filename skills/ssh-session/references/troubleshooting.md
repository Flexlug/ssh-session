# Diagnostics

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

