# Publishing `vesper_core` as a private package

## Assumptions

- package publication goes to a private Python package index.
- credentials are provided through environment variables or CI secrets.

## One-time local setup

Install release tooling from the package's optional `release` extra:

```bash
cd /path/to/vesper
python3 -m pip install -e ".[release]"
```

If you prefer an isolated tool directory instead of an editable install:

```bash
cd /path/to/vesper
python3 -m pip install --target /tmp/vesper-release-tools ".[release]"
```

## Build release artifacts

From the repository root:

```bash
cd /path/to/vesper
python3 -m build --sdist --wheel
```

Expected artifacts:

- `dist/vesper_core-<version>.tar.gz`
- `dist/vesper_core-<version>-py3-none-any.whl`

## Validate artifacts before upload

```bash
cd /path/to/vesper
python3 -m twine check dist/*
```

Optional isolated validation install:

```bash
rm -rf /tmp/vesper-core-validate
python3 -m pip install --no-deps --target /tmp/vesper-core-validate dist/vesper_core-*.whl
python3 -c 'import sys; sys.path.insert(0, "/tmp/vesper-core-validate"); import vesper_core; print(sorted(vesper_core.__all__))'
```

## Upload to a private index

Example using an explicit repository URL:

```bash
cd /path/to/vesper
export TWINE_USERNAME="__token__"
export TWINE_PASSWORD="<private-package-token>"
python3 -m twine upload --repository-url "$VESPER_CORE_REPOSITORY_URL" dist/*
```

If your CI uses a named repository from `.pypirc`, the upload command becomes:

```bash
cd /path/to/vesper
python3 -m twine upload --repository vesper-private dist/*
```

## Release checklist

Before uploading:

1. update `version` in `pyproject.toml`
2. run the unit tests
3. build `sdist` and `wheel`
4. run `twine check`
5. validate installation from the built wheel
6. upload to the private index
