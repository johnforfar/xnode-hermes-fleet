{
  description = "hermes-ollama — shared LLM server for the xnode-hermes-fleet";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Separate always-latest nixpkgs JUST for the ollama package.
    #
    # Why: the openmesh-cli wrapper forces `nixpkgs.follows = "openclaw/nixpkgs"`
    # for the host configuration (Lesson #9 — required for dhcpcd to start
    # inside the container). openclaw/nixpkgs is pinned to an older revision
    # whose `pkgs.ollama` is too old to pull the qwen3.5:* model family
    # (returns HTTP 412 "model requires newer ollama" — Lesson #14).
    #
    # We can't bump openclaw without breaking dhcpcd, so instead we declare
    # this second nixpkgs input that the wrapper does NOT override (it only
    # follows the input literally named `nixpkgs`), and pull only the
    # ollama derivation from it. Everything else still uses openclaw's pkgs.
    #
    # Ollama version landscape (2026-04-13):
    #   - openclaw/nixpkgs (baseline)  → too old, fails 412 on qwen3.5
    #   - nixos-unstable              → ollama v0.20.3 (qwen3.5 OK) ← Test A
    #   - nixpkgs master              → ollama v0.20.5 (qwen3.5 OK)
    #   - upstream latest             → v0.20.6 (Test B via overrideAttrs)
    #
    # v0.19.0 added qwen3.5 manifest support, so anything ≥v0.19 works.
    # nixos-unstable is the safe baseline; we layer a v0.20.6 override on
    # top via the `overrideAttrs` block in the module below.
    nixpkgs-ollama.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = inputs: {
    nixosModules = {
      default =
        { pkgs, lib, ... }:
        let
          # Pull `ollama` from the newer nixpkgs while keeping every other
          # package on the openclaw baseline.
          recentOllama = inputs.nixpkgs-ollama.legacyPackages.${pkgs.system}.ollama;
        in
        {
          config = {
            # Ollama serves models on port 11434.
            # Bind to 0.0.0.0 so sibling containers (hermes-dashboard,
            # hermes-agent-*) can reach it via `hermes-ollama.local:11434`.
            #
            # The mkForce DynamicUser=false + StateDirectory pattern is
            # required for ollama to create its state dir under
            # openclaw/nixpkgs (the wrapper's pinned version).
            # See openmesh-cli/ENGINEERING/PIPELINE-LESSONS.md Lesson #5.
            systemd.services.ollama.serviceConfig.DynamicUser = lib.mkForce false;
            systemd.services.ollama.serviceConfig.ProtectHome = lib.mkForce false;
            systemd.services.ollama.serviceConfig.StateDirectory = [ "ollama/models" ];
            services.ollama = {
              enable = true;
              # v1.9: override the package with the newer ollama from
              # nixpkgs-ollama input (see input declaration above).
              package = recentOllama;
              user = "ollama";
              host = "0.0.0.0";
              port = 11434;
              # v1.9: just preload hermes3:3b (the safe baseline). qwen3.5:4b
              # gets pulled on demand via the dashboard model picker once
              # we've confirmed the new ollama package starts cleanly.
              # Preloading a 3.4GB model on first boot can mask startup
              # failures, so we test the runtime first and pull second.
              loadModels = [ "hermes3:3b" ];
            };

            # Open the firewall so other containers on the host's vz-* bridge
            # can reach 11434.
            networking.firewall.allowedTCPPorts = [ 11434 ];
          };
        };
    };
  };
}
