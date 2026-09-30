# Security

## Reporting a vulnerability

Report vulnerabilities privately: open the repository's **Security** tab on GitHub and choose **Report a vulnerability** (GitHub private vulnerability reporting). Please do not open a public issue, discussion, or pull request for a security problem.

Include what you found, how to reproduce it, the version (`smitline --version`, or the image tag), and what an attacker could do with it. We will acknowledge the report and keep you updated in the private advisory while we work on a fix.

## Why it matters here

Smitline holds your OpenAI and SignalWire or Twilio API keys, and it places phone calls that cost money on those accounts. It also keeps transcripts, call records, the owner's profile, and meeting browser profiles. Reports about any of these are in scope, for example:

- a way to read keys, transcripts, or records without the local token;
- a way to start, steer, or take over a call from outside this computer;
- a way to make it call numbers the owner did not intend, or to bypass the calling-code allow list or the AI disclosure.

## How it is set up by default

- Keys stay in the local data volume (the `smitline` Docker volume, or the gitignored `.env` in a checkout). They are typed into a one-time local page, never into an agent chat, and are never returned to the browser.
- The runtime daemon and the console with its MCP endpoint (`127.0.0.1:8095/mcp`) listen on loopback only and require a bearer token. Public binds are refused unless server mode is turned on with a long-lived API token.
- Only the phone gateway's provider routes, which check the provider's signatures, are reachable through the tunnel.

See the security model in [README.md](README.md#security-model).
