from __future__ import annotations

from pathlib import Path

from .filesystem_helpers import make_safe_component
from .jenkins_helpers import parse_job_name_and_run_number


def make_output_csv_path(pipeline_execution_url, target_output_directory) -> Path:
    """Create the output CSV path as <output_dir>/<pipeline_job_name>-<pipeline_run_number>.csv."""
    job_display_name, job_run_number = parse_job_name_and_run_number(pipeline_execution_url)
    safe_job_label = make_safe_component(job_display_name, default_value="unknown_pipeline", allow_dots=True)
    safe_run_label = str(job_run_number).strip() or "unknown_run"
    return Path(target_output_directory) / f"{safe_job_label}-{safe_run_label}.csv"


def make_pipeline_cache_scope_dirname(pipeline_execution_url) -> str:
    """Create a cache-scope directory name: <pipeline_job_name>-<pipeline_run_number>."""
    job_display_name, job_run_number = parse_job_name_and_run_number(pipeline_execution_url)
    safe_job_label = make_safe_component(job_display_name, default_value="unknown_pipeline", allow_dots=True)
    safe_run_label = str(job_run_number).strip() or "unknown_run"
    return f"{safe_job_label}-{safe_run_label}"

