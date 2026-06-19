# downloader

Independent CLI application that resolves Jenkins job URLs or pipeline URLs, downloads ZIP artifacts, and writes a local `manifest.json`.

The `manifest.json` schema is owned by the internal shared package `vesper_core`.

## Behavior

- If input URL already points to a concrete job run, it is used as-is.
- If input URL points only to a Jenkins job, the app resolves the latest run using `input.build_selector`.
- In `pipeline_url` mode the app resolves the root run, traverses downstream runs, and downloads artifacts for each discovered run.
- In `job_urls` mode the app processes each input independently, deduplicates identical resolved run URLs, and preserves all original source URLs in `manifest.json`.
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

- [`manifest.example.json`](./manifest.example.json) — `pipeline_url` mode
- [`manifest.job-url-list.example.json`](./manifest.job-url-list.example.json) — `job_url_list` mode

If you want to build another application that works together with `downloader`, treat `manifest.json` as the integration contract.
`downloader` is the producer of that contract; strict schema loading and validation live in `vesper_core`, not in Jenkins-specific code.

### Top-level fields

- `scope_name` — scope directory name under `output_root`
- `source_mode` — either `pipeline_url` or `job_url_list`
- `requested_urls` — original URLs from config input
- `resolved_root_run_url` — root pipeline run in `pipeline_url` mode, `null` in `job_url_list` mode
- `build_selector` — selector used when resolving non-concrete job URLs
- `failed_urls` — URLs that could not be resolved or downloaded
- `runs` — successful Jenkins runs that were materialized locally

### Per-run fields

- `job_name` — Jenkins display name, including folder nesting
- `job_run_number` — numeric build number stored as a string
- `run_url` — normalized Jenkins run URL with trailing slash
- `requested_by_urls` — original input URLs that led to this run; in `job_url_list` mode multiple items may point to the same resolved run
- `run_directory` — directory name relative to the scope directory
- `artifact_count` — number of downloaded artifacts listed in `artifacts`

### Per-artifact fields

- `artifact_type` — currently `allure-report` or `zip`
- `file_name` — artifact basename as reported by Jenkins
- `source_relative_path` — Jenkins artifact path inside the run
- `relative_path` — local path relative to the scope directory; this is the safest field for consumers that need to open the file
- `download_url` — original Jenkins artifact URL

### Consumer recommendations

- Open files as `<scope_dir>/<artifact.relative_path>`.
- Use `run_directory` and `relative_path` as relative values; do not assume absolute paths are portable between hosts.
- Do not reconstruct artifact paths from `job_name`; prefer the explicit `relative_path` value.
- Expect `failed_urls` to be non-empty even when `runs` contains valid data.
- Expect `artifact_count` to be smaller than the total Jenkins artifact count when `output.download_only_zip=true`.

### Mode differences

- In `pipeline_url` mode, `requested_urls` usually contains one pipeline URL and every run keeps that same value in `requested_by_urls`.
- In `job_url_list` mode, `resolved_root_run_url` is `null`, and `requested_by_urls` helps consumers understand which original job URLs were deduplicated into the same resolved run.
- Compare the two example files above if you need a concrete integration fixture for both modes.

## Config

See `config.template.json`.

`input.job_urls_skip` should be placed next to `input.job_urls` and uses job-level matching:

- `http://jenkins:8080/job/example-job/` skips all runs of that job
- `http://jenkins:8080/job/example-job/153/` also skips the whole `example-job`

## Run

```bash
python -m downloader.main --config /path/to/downloader-config.json
```
