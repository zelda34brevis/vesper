# reporter

Independent CLI application that reads a local `manifest.json` produced by `downloader`, parses `allure-report.zip`, matches per-test log ZIP archives by filename, extracts backtraces, and writes a CSV report.

## Behavior

- Uses only local files from a downloader scope directory.
- Never talks to Jenkins.
- Missing or malformed per-run artifacts are handled according to `parsing.partial_mode`:
  - `warn` — log and continue.
  - `fail` — stop immediately.
- Test-log ZIP archives are matched by manifest index plus artifact filename.

## Input

- Either `input.manifest_path`
- Or `input.scope_dir` (the app will read `<scope_dir>/manifest.json`)

## Run

```bash
python -m reporter.main --config /path/to/reporter-config.json
```

