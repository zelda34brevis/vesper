# vesper-core

Shared manifest contract and neutral utilities for applications that consume artifacts produced by the Vesper downloader.

## Included modules

- `vesper_core.manifest` — strict manifest data model and validation
- `vesper_core.text` — safe text helpers for filesystem-friendly names

## Install

```bash
python3 -m pip install vesper-core
```

For local editable development from this repository:

```bash
python3 -m pip install -e /path/to/vesper/vesper_core
```

## Build

```bash
make clean
make bootstrap-tools
make build-wheel
make build-sdist
make build
make check-dist
```

The `Makefile` bootstraps a local `.release-venv` with `build` and `twine` automatically.
