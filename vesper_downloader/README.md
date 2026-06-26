# vesper-downloader

Independent CLI application that resolves Jenkins job URLs or pipeline URLs, downloads ZIP artifacts, and writes a local `manifest.json`.

The `manifest.json` schema is owned by `vesper-core`, which is now published as a separate dependency.

## Behavior

- If input URL already points to a concrete job run, it is used as-is.
- If input URL points only to a Jenkins job, the app resolves the latest run using `input.build_selector`.
- In `pipeline_url` mode the app resolves the root run, traverses downstream runs, and downloads artifacts for each discovered run.
- In `job_url_list` mode the app processes each input independently, deduplicates identical resolved run URLs, and preserves all original source URLs in `manifest.json`.
- `input.job_urls_skip` accepts Jenkins job URLs (or explicit run URLs that belong to those jobs) and excludes every matching run from both modes.
- If `input.pipeline_url` itself belongs to a skipped job, or if the skip filtering leaves nothing to process, the downloader exits with an error.
- By default only `.zip` artifacts are downloaded.

## Output layout

```text
<output_root>/<scope_name>/
  manifest.json
  <job_name>-<run_number>/
    <artifact files preserved by Jenkins relativePath>
```

## Manifest contract

Example payloads:

- [`manifest.example.json`](./vesper_downloader/manifest.example.json) — `pipeline_url` mode
- [`manifest.job-url-list.example.json`](./vesper_downloader/manifest.job-url-list.example.json) — `job_url_list` mode

If you want to build another application that works together with `vesper-downloader`, treat `manifest.json` as the integration contract. `vesper-downloader` is the producer of that contract; strict schema loading and validation live in `vesper_core`, not in Jenkins-specific code.

## Config

See `vesper_downloader/config.template.json`.

## Install

```bash
python3 -m pip install vesper-downloader
```

For local editable development from this repository:

```bash
python3 -m pip install -e /path/to/vesper/vesper_core
python3 -m pip install -e /path/to/vesper/vesper_downloader
```

## Run

```bash
vesper-downloader --config /path/to/downloader-config.json
```

Or without installing the console script:

```bash
python -m vesper_downloader.main --config /path/to/downloader-config.json
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
