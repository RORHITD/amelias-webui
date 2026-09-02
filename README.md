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

## Docker

Pre-built images (amd64 + arm64) are published to GHCR on every release. Run Compose as the
user who owns your Hermes home — `sudo docker compose up -d` can make `${HOME}` expand to the root user's home, so Docker mounts the wrong `.hermes` directory instead of your real `~/.hermes` and the WebUI starts with `config.yaml (not found, using defaults)`. Prefer adding
your user to the Docker group and running `docker compose up -d`; if you must use sudo, set
absolute paths first, for example `HERMES_HOME=/home/you/.hermes HERMES_WORKSPACE=/home/you/workspace sudo -E docker compose up -d`, then verify with
`docker compose config`.

**Common failure modes**

| Symptom | Likely cause | Fix |
|---|---|---|
| `PermissionError` at startup | UID mismatch on bind mount | Set `UID=$(id -u)` in `.env` |
| Workspace appears empty | UID mismatch on `/workspace` mount | Set `UID=$(id -u)` in `.env` |
| Host API at `localhost` fails from WebUI | Container `localhost` means the container, not your host (#3012) | Use `http://host.docker.internal:<port>` on Docker Desktop, or `http://host.containers.internal:<port>` on Podman |
| WebUI can't see `~/.hermes` after `sudo docker compose` | `${HOME}` expanded to the root user's home (#3006) | Run Compose as your user, or pass absolute `HERMES_HOME`/`HERMES_WORKSPACE` with `sudo -E` |

For the full setup guide (all 3 compose files, bind-mount migration, and the rest of the
failure modes), see [`docs/docker.md`](docs/docker.md).

### Nix flake and NixOS module

Hermes WebUI has a Nix flake package and a NixOS service module so you can run it declaratively.
Install the latest package with `nix shell github:RORHITD/amelias-webui#default`, or wire the
flake input into a system configuration:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # The agent side has no flake of its own in our pinned mirror, so this
    # points at the upstream project that publishes one.
    hermes-agent.url = "github:NousResearch/hermes-agent";
    hermes-webui.url = "github:RORHITD/amelias-webui";
  };

  outputs = { self, nixpkgs, hermes-agent, hermes-webui, ... }: {
    nixosConfigurations.<host> = nixpkgs.lib.nixosSystem {
      modules = [
        hermes-agent.nixosModules.default
        hermes-webui.nixosModules.default
        ({ pkgs, ... }: {
          services.hermes-agent.enable = true;
          services.hermes-webui = {
            enable = true;
            host = "127.0.0.1";
            port = 8787;
            stateDir = "/var/lib/hermes-webui";
            user = "hermes";
            group = "hermes";
            hermesHome = "/var/lib/hermes/.hermes";
            agent.package = hermes-agent.packages.${pkgs.stdenv.hostPlatform.system}.default;
            environmentFiles = [ "/run/secrets/hermes-webui.env" ];
          };
        })
      ];
    };
  };
}
```

The module defaults to `127.0.0.1`. Set `host = "0.0.0.0"` and `openFirewall = true` only when
you want direct network access, and pair that with auth, for example `HERMES_WEBUI_PASSWORD`
via `environmentFiles`.

### Remote access (SSH tunnel, Tailscale, phone)

The server binds to `127.0.0.1` by default. Reach it from another machine over an SSH tunnel
(`ssh -N -L 8787:127.0.0.1:8787 user@host`), or by joining server and phone to a Tailscale
network as shown in Quick start above. Full walkthrough: [`docs/remote-access.md`](docs/remote-access.md).

## Compatibility

The version shown in the WebUI runtime status is the **WebUI version only** (build/image/tag
currently running). It is not a full compatibility map.

The WebUI is still coupled to Hermes Agent internals for runtime execution, provider/model
access, and state/schema usage. Upstream is tracking the work to make that a stable API boundary
instead in [nesquena/hermes-webui#1925](https://github.com/nesquena/hermes-webui/issues/1925)
and [#2491](https://github.com/nesquena/hermes-webui/issues/2491). In practice, the WebUI
imports Agent modules directly (`api/config.py`, `api/providers.py`, `api/streaming.py`) and
reads Agent state layout directly, so version skew can cause import or behavior drift — and this
bites a pinned fork like this one *harder* than it bites upstream, since we don't rebase onto
every upstream release the moment it lands.

**Compatibility policy**
- WebUI release branches are tested against the matching Hermes Agent release available at that
  WebUI release time.
- **Upgrade both together**: upgrade or pin WebUI and hermes-agent together (same release
  train/version/date), especially before enabling production traffic.
- Running pinned older/newer combinations is **untested and unsupported** until the stable API
  boundary work above is in place.
- Record the full `hermes-agent` + `hermes-webui` versions in issue reports when upgrade
  mismatches are suspected.

**Docker users**: pin both image tags (or corresponding pinned source revisions) rather than
using `latest` on one side and a fixed tag on the other. When upgrading the multi-container
setup, follow the agent-image upgrade procedure in [`docs/docker.md`](docs/docker.md) (which
requires dropping the `hermes-agent-src` volume before recreating). The current source-boundary
status is tracked in [`docs/rfcs/agent-source-boundary.md`](docs/rfcs/agent-source-boundary.md).

## Docs

**Start here**
- [`docs/why-hermes.md`](docs/why-hermes.md) — why Hermes, the mental model, and a detailed
  comparison to Claude Code / Codex / OpenCode / Cursor
- [`docs/onboarding.md`](docs/onboarding.md) — first-run wizard, provider setup, local model
  server Base URLs, and safe re-runs
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — diagnostic flows for common failures

**Using & customizing**
- [`THEMES.md`](THEMES.md) — theme + skin system, custom theme guide
- [`docs/workspace-git.md`](docs/workspace-git.md) — the workspace Git controls
- [`docs/EXTENSIONS.md`](docs/EXTENSIONS.md) — administrator-controlled WebUI extension injection

**Deploying & operating**
- [`docs/remote-access.md`](docs/remote-access.md) — SSH tunnel, Tailscale, and phone access
- [`docs/docker.md`](docs/docker.md) — Docker compose setup, common failures, and bind-mount
  migration
- [`docs/supervisor.md`](docs/supervisor.md) — launchd, systemd, supervisord, runit, and s6
  process-supervisor setup
- [`docs/wsl-autostart.md`](docs/wsl-autostart.md) — WSL2 auto-start at Windows login

**Contributing & design**
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution style, PR expectations, and local
  verification
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system design, all API endpoints, implementation notes
- [`DESIGN.md`](DESIGN.md) — design tokens and the calm-console direction

## Provenance & license

This repository is a pinned, self-maintained distribution based on
[nesquena/hermes-webui](https://github.com/nesquena/hermes-webui) (MIT), with Amelias Agent's
own defaults: update checks and links point here, and the agent-engine installer resolves to
[RORHITD/amelias-agent](https://github.com/RORHITD/amelias-agent). The original README ships
unchanged at [`docs/UPSTREAM-README.md`](docs/UPSTREAM-README.md). MIT license preserved — see
[`LICENSE`](LICENSE) and [`CONTRIBUTORS.md`](CONTRIBUTORS.md) for the community credit roll.
