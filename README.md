# xnode-hermes-fleet

A multi-agent task board running entirely on a sovereign Openmesh Xnode.
Inspired by paperclip's "agents pick up cards" idea but rebuilt from scratch
in ~1500 lines of Python + nix + vanilla JS, with no upstream dependency
on either paperclip or hermes-agent (yet — see "v2 upgrade path" below).

Live demo: **https://hermes.build.openmesh.cloud**

## What it does

You drop tasks into a kanban backlog. Specialist agent containers
(`pm`, `coder`, `researcher`, `writer`) poll the board, atomically claim
the oldest task tagged for their role, run it through a shared local LLM
(`hermes3:3b` on ollama), and write the result back. The dashboard
streams live updates so you watch cards move from `backlog` →
`in_progress` → `done` in real time.

No SaaS. No telemetry. No API keys. Local LLM. Local postgres. Local
nginx. The whole thing fits in ~3.5 GB of RAM on a single 8-vCPU
sovereign Xnode.

## Architecture

```
                         hermes-ollama
                  (hermes3:3b loaded once, shared)
                              ▲
                              │ http
            ┌─────────────────┼─────────────────┐
            │                 │                 │
   hermes-agent-pm   hermes-agent-coder   hermes-agent-...
   (python worker)   (python worker)     (python worker)
            │                 │                 │
            └─────────────────┼─────────────────┘
                              ▼ postgres
                     hermes-dashboard
                  (kanban UI + REST API)
                              │
                              ▼ public TLS
              hermes.build.openmesh.cloud
```

Each box is a separate **nixos-container** managed by `om`. They reach
each other via the host's `vz-*` bridge using `<container-name>.local`
DNS (per the xnode-manager DNS convention — see
`openmesh-cli/ENGINEERING/PIPELINE-LESSONS.md`).

## Repo layout

```
xnode-hermes-fleet/
├── ollama/
│   └── flake.nix           # services.ollama with hermes3:3b
├── dashboard/
│   ├── flake.nix           # postgres + python flask + nginx
│   ├── backend/app.py      # ~430 lines: REST + SSE
│   └── frontend/index.html # vanilla JS kanban with aurora theme
├── agent/
│   ├── flake.nix           # python worker as systemd service
│   └── worker/worker.py    # ~210 lines: poll, claim, infer, write
└── README.md               # this file
```

Three independently-deployable flakes (ollama, dashboard, agent), one
worker codebase shared by all four agent containers.

## Deploy

Requires the [Openmesh CLI](https://github.com/johnforfar/openmesh-cli)
v2.0+ and an authenticated session (`om login`).

```bash
# 1. Shared model server (~5-10 min first time, downloads hermes3:3b)
om app deploy hermes-ollama \
  --flake github:johnforfar/xnode-hermes-fleet?dir=ollama

# 2. Dashboard with postgres
om app deploy hermes-dashboard \
  --flake github:johnforfar/xnode-hermes-fleet?dir=dashboard

# 3. Public URL on your subdomain
om app expose hermes-dashboard \
  --domain agents.<your-subdomain>.openmesh.cloud \
  --port 8080

# 4. The four agents (each a separate container running the same worker code)
om app deploy hermes-agent-pm         --flake github:johnforfar/xnode-hermes-fleet?dir=agent
om app deploy hermes-agent-coder      --flake github:johnforfar/xnode-hermes-fleet?dir=agent
om app deploy hermes-agent-researcher --flake github:johnforfar/xnode-hermes-fleet?dir=agent
om app deploy hermes-agent-writer     --flake github:johnforfar/xnode-hermes-fleet?dir=agent
```

Each agent container reads its **role from its own hostname** — `hermes-agent-pm`
becomes role `pm`, looks up its system prompt in the dashboard's postgres,
and starts polling for `pm`-tagged tasks.

## Adding a new agent role

1. Pick a name like `hermes-agent-translator`
2. Optional: add a default system prompt to `dashboard/backend/app.py`
   in the `DEFAULT_AGENTS` list (or let the agent self-register with a
   placeholder prompt)
3. Deploy:
   ```bash
   om app deploy hermes-agent-translator \
     --flake github:johnforfar/xnode-hermes-fleet?dir=agent
   ```
4. The new agent appears in the dashboard's agent strip immediately

## Resource budget on the live xnode

Measured on `manager.build.openmesh.cloud` (8 vCPU, 16 GB RAM):

| Container | RAM | Notes |
|---|---|---|
| `hermes-ollama` | ~2.5 GB | hermes3:3b loaded once |
| `hermes-dashboard` | ~400 MB | postgres + python + nginx |
| `hermes-agent-*` (each) | ~150 MB | thin python polling loop |
| **Fleet total** | **~3.5 GB** | with 4 agents |

## Security model

- **No passwords on postgres** — uses `trust` auth restricted to the
  `192.168.0.0/16` container subnet (the host's `vz-*` bridge). The
  host firewall blocks 5432 from any external traffic.
- **The python worker runs as a non-root system user** (`hermes`).
- **No public exposure** of postgres or ollama. Only the dashboard
  (port 8080) is reachable from outside via the host nginx + TLS.
- **No secrets in source.** This whole repo is OK to publish public.

## v2 upgrade path

This is intentionally a **v1 from-scratch** build. The custom worker is
~210 lines and gives full control over the schema, UI, and task
semantics. Tradeoff: we're reimplementing things that
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
already provides (the actual agent logic, tool use, memory).

When the upstream `services.hermes-agent` NixOS module's API stabilizes,
the `agent/` flake should be replaced with:

```nix
inputs.hermes.url = "github:NousResearch/hermes-agent/v2026.3.30";
# ...
modules = [
  inputs.xnode-manager.nixosModules.container
  inputs.hermes.nixosModules.default
  ({ ... }: {
    services.hermes-agent.enable = true;
    # plus a bridge that pulls tasks from the dashboard's postgres
  })
];
```

The dashboard, ollama container, and database schema would all stay
identical. Only the agent container's internals change.

## Credits

- **Openmesh** for the sovereign Xnode infrastructure
- **NousResearch** for hermes3 and the agent ecosystem we'll integrate later
- **Paperclip** for the multi-agent kanban inspiration
- **Openmesh CLI** ([johnforfar/openmesh-cli](https://github.com/johnforfar/openmesh-cli))
  for making the deploy a 4-command setup

---

*Deployed with Openmesh CLI v2.0 via Claude Code — [John Forfar](https://github.com/johnforfar)*
