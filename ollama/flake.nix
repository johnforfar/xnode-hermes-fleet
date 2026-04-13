{
  description = "hermes-ollama — shared LLM server for the xnode-hermes-fleet";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = inputs: {
    nixosModules = {
      default =
        { pkgs, lib, ... }:
        {
          config = {
            # Ollama serves the hermes3:3b model on port 11434.
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
              user = "ollama";
              host = "0.0.0.0";
              port = 11434;
              # v1.9: qwen3.5:4b is the new default — multimodal, thinking,
              # ~3.4GB on disk, ~2.6GB RAM when loaded. hermes3:3b stays
              # preloaded as a fallback option in the model picker.
              loadModels = [ "qwen3.5:4b" "hermes3:3b" ];
            };

            # Open the firewall so other containers on the host's vz-* bridge
            # can reach 11434.
            networking.firewall.allowedTCPPorts = [ 11434 ];
          };
        };
    };
  };
}
