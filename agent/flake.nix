{
  description = "hermes-fleet agent worker — one container per role";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = inputs: {
    nixosModules = {
      default =
        { pkgs, lib, ... }:
        let
          pythonEnv = pkgs.python3.withPackages (
            ps: with ps; [
              psycopg2
              requests
            ]
          );

          workerScript = pkgs.writeText "hermes-agent-worker.py" (
            builtins.readFile ./worker/worker.py
          );
        in
        {
          config = {
            # Each agent container is a thin polling worker. It does NOT
            # run its own postgres or ollama — both are reached via the
            # host's vz-* bridge from sibling containers:
            #   - hermes-dashboard.local:5432  (postgres)
            #   - hermes-ollama.local:11434    (ollama, hermes3:3b)
            #
            # The agent's role is derived from the container hostname:
            # `hermes-agent-pm` → role "pm". The systemd unit reads
            # /etc/hostname (or the env var AGENT_NAME) at startup.
            #
            # For v2: replace this entire systemd service with
            # `services.hermes-agent.enable = true` from
            # `nousresearch/hermes-agent/v2026.3.30` and a thin adapter
            # that bridges hermes-agent's task model to the dashboard's
            # postgres schema. See ENGINEERING/PIPELINE-LESSONS.md.
            users.users.hermes = {
              isSystemUser = true;
              group = "hermes";
            };
            users.groups.hermes = { };

            systemd.services.hermes-agent-worker = {
              description = "hermes-fleet agent worker";
              after = [ "network.target" ];
              wantedBy = [ "multi-user.target" ];

              environment = {
                # Connect to the dashboard's postgres via TCP. Auth is
                # `trust` for our subnet (192.168.0.0/16) so no password.
                DATABASE_URL = "postgresql://hermes@hermes-dashboard.local:5432/hermes";
                OLLAMA_URL = "http://hermes-ollama.local:11434";
                CHAT_MODEL = "qwen3.5:4b";  # fallback only — worker reads settings.active_model
                POLL_INTERVAL = "5";
                NUM_PREDICT = "400";
                TEMPERATURE = "0.5";
                # AGENT_NAME is auto-derived from /etc/hostname inside
                # the worker (e.g. `hermes-agent-pm` → `pm`). Override
                # by setting AGENT_NAME explicitly.
              };

              serviceConfig = {
                Type = "simple";
                ExecStart = "${pythonEnv}/bin/python ${workerScript}";
                Restart = "on-failure";
                RestartSec = "10s";
                User = "hermes";
                Group = "hermes";
              };
            };
          };
        };
    };
  };
}
