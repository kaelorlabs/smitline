# smitline CLI

Command-line client for the local Smitline daemon: phone calls and Zoom, Teams, and Google Meet meetings from a brief, plus setup. It wraps the TypeScript SDK and talks only to the loopback daemon.

It comes with the `smitline` image, where you run it with `docker exec smitline smitline ...` (the examples below leave out the `docker exec smitline` part). From a checkout it needs Node.js 22+ and runs as `node packages/cli/src/smitline.mjs`. Not published to npm. Run `smitline help` for every option.

## Phone calls

```bash
smitline call --to +14155550142 --objective "Book a table for two at 7pm Friday" \
  --agree "6:30pm; 7:30pm" --never-share "card number" --wait
```

## Meetings

A meeting is a call on the `meeting` channel whose `to` is the invite URL. Any of these joins it:

```bash
smitline call --meeting "https://zoom.us/j/123456789" --objective "Take notes on the roadmap review" --wait
smitline call --channel meeting --to "https://zoom.us/j/123456789" --objective "Take notes on the roadmap review"
smitline call --to "https://meet.google.com/abc-defg-hij" --objective "Take notes on the roadmap review"
```

A `--to` that starts with `http://` or `https://` is treated as a meeting.

## Following a call

`call` returns at once with the call id; `--wait` follows it until it ends and prints the result JSON on stdout, with one progress line per status change on stderr. Ctrl-C stops following; the call keeps going.

```bash
smitline calls list
smitline calls wait --call-id <id>
smitline calls instruct --call-id <id> --text "Ask whether Friday works" [--silent]
smitline calls end --call-id <id>
smitline calls transfer --call-id <id>
```

Other commands: `smitline profile`, `smitline voices`, `smitline setup …`, and `smitline connector …`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Done (for `--wait`: the call completed) |
| 2 | Validation / usage; an incomplete brief prints a question for each missing field |
| 3 | Startup (daemon) or setup not ready |
| 4 | Runtime failure, or the call did not complete |
| 130 | Interrupted |

Tokens and other secrets are never written to argv diagnostics, logs, or progress lines.
