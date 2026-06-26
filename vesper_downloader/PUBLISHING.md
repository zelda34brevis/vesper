# Publishing `vesper-downloader`

`vesper-downloader` depends on `vesper-core`, so publish or update `vesper-core` first when the downloader needs a newer core release.

## Tooling setup

`make` bootstraps a local `.release-venv` with `build` and `twine` automatically. If you want to pre-create it:

```bash
cd /path/to/vesper/vesper_downloader
make bootstrap-tools
```

## Build release artifacts

```bash
cd /path/to/vesper/vesper_downloader
make build
```

Expected artifacts:

- `dist/vesper_downloader-<version>.tar.gz`
- `dist/vesper_downloader-<version>-py3-none-any.whl`

## Validate artifacts before upload

```bash
cd /path/to/vesper/vesper_downloader
make check-dist
```

## Upload

```bash
cd /path/to/vesper/vesper_downloader
make publish TWINE_ARGS="--repository-url https://packages.example/simple/"
```

For a named repository from `.pypirc`:

```bash
cd /path/to/vesper/vesper_downloader
make publish TWINE_ARGS="--repository vesper-private"
```
