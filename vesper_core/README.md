# Summary

Vesper downloader gets the artifacts from CI, stores them in a structured way, and provide a manifest.json for consumers.

Vesper core is a library that provides a common contract for working with the artifacts and manifest produced by the downloader, and is intended to be used by consumers.

## Packaging

- The repository root `pyproject.toml` builds/installs `vesper_core`.
- Private-package publishing steps are documented in [`PUBLISHING.md`](./PUBLISHING.md).

```bash
python3 -m pip install --no-deps --target /tmp/vesper-core-test /path/to/vesper
```
