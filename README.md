# vesper

This project now contains two independent applications:

- `downloader/` — resolves Jenkins job runs, downloads ZIP artifacts, writes `manifest.json`
- `reporter/` — reads local downloaded artifacts and generates a CSV report

It also contains one internal shared package:

- `vesper_core/` — shared manifest contract, strict validation, path helpers, and neutral filesystem/text utilities

The repository root `pyproject.toml` is intentionally scoped to packaging `vesper_core` only, so the shared contract can be extracted later without dragging app-specific code with it.

## Independence model

- `downloader` and `reporter` are designed to run on different hosts.
- They exchange data only through a scope directory and `manifest.json`.
- `reporter` has no Jenkins dependency.

## Directory contract

```text
<output_root>/<scope_name>/
  manifest.json
  <job_name>-<run_number>/
    ...downloaded ZIP artifacts...
```

## Integration notes

- `manifest.json` is the hand-off contract between `downloader` and any paired application.
- The contract is implemented in code by `vesper_core.Manifest` and related dataclasses.
- Shared-core responsibilities and boundaries are documented in [`vesper_core/README.md`](./vesper_core/README.md).
- Concrete examples are available at [`downloader/manifest.example.json`](./downloader/manifest.example.json) for `pipeline_url` mode and [`downloader/manifest.job-url-list.example.json`](./downloader/manifest.job-url-list.example.json) for `job_url_list` mode.
- Field-by-field integration details are documented in [`downloader/README.md`](./downloader/README.md).

## Shared core boundaries

- `vesper_core` must stay independent from Jenkins, Allure, and CSV/report-specific logic.
- `downloader` owns Jenkins resolution and artifact downloading.
- `reporter` owns Allure parsing, backtrace extraction, and CSV generation.
- Shared code is limited to the manifest schema, strict manifest I/O/validation, and neutral helpers that keep both applications aligned.
