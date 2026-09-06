# How it works

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
