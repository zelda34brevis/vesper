# downloader

Independent CLI application that resolves Jenkins job URLs or pipeline URLs, downloads ZIP artifacts, and writes a local `manifest.json`.

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

## Config

See `config.template.json`.

`input.job_urls_skip` should be placed next to `input.job_urls` and uses job-level matching:

- `http://jenkins:8080/job/example-job/` skips all runs of that job
- `http://jenkins:8080/job/example-job/153/` also skips the whole `example-job`

## Run

```bash
python -m downloader.main --config /path/to/downloader-config.json
```
