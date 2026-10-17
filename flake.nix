{
  description = "m5stack-sandbox の開発環境 (uv の外で入れる CLI ツール)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAllSystems (pkgs: {
        # Python 側 (uv・mpy-cross・mpremote など) は uv が管理するので載せない
        default = pkgs.mkShellNoCC {
          packages = with pkgs; [
            # poe license がライセンス検査に使う
            trivy
          ];
        };
      });
    };
}
