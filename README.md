# vesper

Repository with two independently publishable Python packages:

- `vesper_core` — shared manifest contract and neutral utilities
- `vesper_downloader` — Jenkins artifact downloader CLI built on top of `vesper-core`

## Repository layout

```text
vesper/
  vesper_core/
    pyproject.toml
    Makefile
    vesper_core/
  vesper_downloader/
    pyproject.toml
    Makefile
    vesper_downloader/
```

Each directory above is a standalone Python project with its own build metadata, `dist/` directory, and publishing flow.

Each project `Makefile` bootstraps its own local `.release-venv` with `build` and `twine`, so the build targets do not depend on globally installed packaging tools.

## Build from the repository root

```bash
make build-core
make build-core-wheel
make build-core-sdist
make check-core-dist

make build-downloader
make build-downloader-wheel
make build-downloader-sdist
make check-downloader-dist
```

## Publish separately

Set `TWINE_ARGS` to the repository flag you need and publish either package independently:

```bash
make publish-core TWINE_ARGS="--repository-url https://packages.example/simple/"
make publish-downloader TWINE_ARGS="--repository-url https://packages.example/simple/"
```

`TWINE_ARGS` can also use a named repository, for example `--repository vesper-private`.

## Local development

For editable installs in a fresh environment:

```bash
python3 -m pip install -e ./vesper_core
python3 -m pip install -e ./vesper_downloader
```
