# Amelias Agent — Web UI backend

The server behind the **Amelias Agent** iPhone app ([ameliasagent.com](https://ameliasagent.com)).
It runs on your own Mac, drives your AI agents locally, and the app connects to it over your
private Tailscale network. Your code, chats, and Memory never leave hardware you control.

## Quick start

```bash
git clone https://github.com/RORHITD/amelias-webui.git
cd amelias-webui
python3 bootstrap.py
```

The bootstrap finds (or installs) the agent engine, sets up a Python environment, starts the
server on `127.0.0.1:8787`, and walks you through first-run setup. Health check:

```bash
curl http://127.0.0.1:8787/health
```

Reach it from your iPhone over Tailscale:

```bash
tailscale serve --bg 8787
```

Then put the printed `https://…ts.net` URL into the Amelias Agent app. The app's onboarding
includes a copy-paste prompt that lets any AI agent on your Mac do this whole setup for you.

## Daemon lifecycle

```bash
./ctl.sh start     # background daemon, PID at ~/.hermes/webui.pid
./ctl.sh status
./ctl.sh logs --lines 100
./ctl.sh restart
./ctl.sh stop
```

## Security defaults

- Binds to `127.0.0.1` only; remote access is via Tailscale's private, end-to-end encrypted network.
- Optional password auth: set `HERMES_WEBUI_PASSWORD` in a `chmod 600` `.env`.
- No telemetry. Everything is stored under `~/.hermes/` on your machine.

## Works with any model

Local models via Ollama/LM Studio (OpenAI-compatible `base_url`), or hosted providers
(Anthropic, OpenAI, DeepSeek, MiniMax, Kimi, OpenRouter, …) — configured in Settings → Providers.

## Docs

Everything under [`docs/`](docs/), including remote access, supervisors (launchd/systemd),
Docker, and troubleshooting. Architecture notes: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Provenance & license

This repository is a pinned, self-maintained distribution based on
[nesquena/hermes-webui](https://github.com/nesquena/hermes-webui) (MIT), with Amelias Agent's
own defaults: update checks and links point here, and the agent-engine installer resolves to
[RORHITD/amelias-agent](https://github.com/RORHITD/amelias-agent). The original README ships
unchanged at [`docs/UPSTREAM-README.md`](docs/UPSTREAM-README.md). MIT license preserved — see
[`LICENSE`](LICENSE) and [`CONTRIBUTORS.md`](CONTRIBUTORS.md) for the community credit roll.
