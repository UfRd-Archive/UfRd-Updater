{
  lib,
  python3Packages,
}:

python3Packages.buildPythonApplication {
  pname = "ufrd-updater";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSourceWith {
    src = lib.cleanSource ../.;
    filter =
      path: type:
      let
        base = baseNameOf path;
      in
      !(lib.elem base [
        ".venv"
        ".pytest_cache"
        "dist"
        "result"
        "__pycache__"
      ]);
  };

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    beautifulsoup4
    httpx
    jmespath
  ];

  # CI runs the pytest suite with uv; the Nix sandbox has no network or
  # user-level git configuration, so the build-time check is disabled.
  doCheck = false;

  meta = {
    description = "Config-driven artifact updater for UfRd-Archive";
    homepage = "https://github.com/UfRd-Archive/UfRd-Updater";
    license = lib.licenses.mit;
    mainProgram = "ufrd-updater";
    platforms = lib.platforms.unix;
  };
}
