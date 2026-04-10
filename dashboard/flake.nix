{
  description = "hermes-dashboard — kanban board + REST API for the xnode-hermes-fleet";

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
              flask
              psycopg2
              requests
            ]
          );

          backendApp = pkgs.writeText "hermes-dashboard-app.py" (
            builtins.readFile ./backend/app.py
          );
        in
        {
          config = {
            # ===============================================================
            # Postgres — task store + agent registry
            #
            # Listens on TCP so sibling agent containers (hermes-agent-*)
            # can connect via `hermes-dashboard.local:5432`. Uses trust
            # auth restricted to the host's vz-* bridge subnet
            # (192.168.0.0/16) — internal-only, never exposed publicly.
            # The host firewall blocks 5432 from any external traffic.
            # ===============================================================
            users.users.hermes = {
              isSystemUser = true;
              group = "hermes";
            };
            users.groups.hermes = { };

            services.postgresql = {
              enable = true;
              package = pkgs.postgresql_16;
              enableTCPIP = true;
              # Listen on all interfaces inside the container (the firewall
              # below limits which IPs can actually connect).
              settings = {
                listen_addresses = lib.mkForce "*";
                port = 5432;
              };
              authentication = lib.mkOverride 10 ''
                # TYPE  DATABASE  USER     ADDRESS         METHOD
                local   all       all                      peer
                # Trust connections from sibling containers on the host's
                # vz-* bridge subnet. NOT exposed publicly.
                host    all       all      192.168.0.0/16  trust
                host    all       all      127.0.0.1/32    trust
                host    all       all      ::1/128         trust
              '';
              ensureDatabases = [ "hermes" ];
              ensureUsers = [
                {
                  name = "hermes";
                  ensureDBOwnership = true;
                }
              ];
            };

            networking.firewall.allowedTCPPorts = [ 5432 8080 ];

            # ===============================================================
            # Dashboard backend (Python Flask)
            # ===============================================================
            systemd.services.hermes-dashboard = {
              description = "hermes-dashboard backend";
              after = [
                "postgresql.service"
                "network.target"
              ];
              wants = [ "postgresql.service" ];
              wantedBy = [ "multi-user.target" ];

              environment = {
                # Local connection via unix socket (peer auth as `hermes` user).
                DATABASE_URL = "postgresql:///hermes?host=/run/postgresql";
                # Cross-container ollama for direct chat-with-agent feature
                OLLAMA_URL = "http://hermes-ollama:11434";
                CHAT_MODEL = "hermes3:3b";
                PORT = "5000";
                BIND_HOST = "127.0.0.1";
              };

              serviceConfig = {
                Type = "simple";
                ExecStart = "${pythonEnv}/bin/python ${backendApp}";
                Restart = "on-failure";
                RestartSec = "10s";
                User = "hermes";
                Group = "hermes";
              };
            };

            # ===============================================================
            # nginx — serves the kanban frontend on / and proxies /api/*
            # to the Python backend on localhost:5000
            # ===============================================================
            services.nginx = {
              enable = true;
              recommendedGzipSettings = true;
              recommendedOptimisation = true;
              recommendedProxySettings = true;

              virtualHosts."default" = {
                default = true;
                listen = [
                  {
                    addr = "0.0.0.0";
                    port = 8080;
                  }
                ];
                root = "${./frontend}";
                locations."/" = {
                  tryFiles = "$uri /index.html";
                };
                locations."/api/" = {
                  proxyPass = "http://127.0.0.1:5000";
                  extraConfig = ''
                    proxy_buffering off;
                    proxy_cache off;
                    proxy_set_header Connection "";
                    proxy_http_version 1.1;
                    chunked_transfer_encoding on;
                    proxy_read_timeout 300s;
                    proxy_send_timeout 300s;
                  '';
                };
              };
            };
          };
        };
    };
  };
}
