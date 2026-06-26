# Publishing `vesper-core`

## Tooling setup

`make` bootstraps a local `.release-venv` with `build` and `twine` automatically. If you want to pre-create it:

```bash
cd /path/to/vesper/vesper_core
make bootstrap-tools
```

## Build release artifacts

```bash
cd /path/to/vesper/vesper_core
make build
```

Expected artifacts:

- `dist/vesper_core-<version>.tar.gz`
- `dist/vesper_core-<version>-py3-none-any.whl`

## Validate artifacts before upload

```bash
cd /path/to/vesper/vesper_core
make check-dist
```

Optional isolated validation install:

```bash
rm -rf /tmp/vesper-core-validate
python3 -m pip install --no-deps --target /tmp/vesper-core-validate dist/vesper_core-*.whl
python3 -c 'import sys; sys.path.insert(0, "/tmp/vesper-core-validate"); import vesper_core; print(sorted(vesper_core.__all__))'
```

## Upload

```bash
cd /path/to/vesper/vesper_core
make publish TWINE_ARGS="--repository-url https://packages.example/simple/"
```

For a named repository from `.pypirc`:

```bash
cd /path/to/vesper/vesper_core
make publish TWINE_ARGS="--repository vesper-private"
```
