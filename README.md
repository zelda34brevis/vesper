# vesper

This project now contains two independent applications:

- `downloader/` — resolves Jenkins job runs, downloads ZIP artifacts, writes `manifest.json`
- `reporter/` — reads local downloaded artifacts and generates a CSV report

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
- Concrete examples are available at [`downloader/manifest.example.json`](./downloader/manifest.example.json) for `pipeline_url` mode and [`downloader/manifest.job-url-list.example.json`](./downloader/manifest.job-url-list.example.json) for `job_url_list` mode.
- Field-by-field integration details are documented in [`downloader/README.md`](./downloader/README.md).

