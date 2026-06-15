# flake8: noqa: E402
# pylint: disable=wrong-import-position,import-error

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
	sys.path.insert(0, str(SCRIPT_DIRECTORY))

from runtime_context import aggregated_reports_directory
from settings import ROOT_JOBRUN_URL
from utils.downstream_jobruns import resolve_job_url_to_latest_jobrun_url
from utils.jenkins_helpers import has_explicit_run_number
from utils.time_formatting import render_elapsed_time
from utils.pipeline_csv_export import export_multi_jobs_results
from utils.pipeline_report_paths import make_output_csv_path

module_logger = logging.getLogger(__name__)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(
	logging.Formatter('%(asctime)s.%(msecs)03d %(name)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
module_logger.addHandler(stdout_handler)
module_logger.propagate = False


if __name__ == "__main__":
	start_time = time.perf_counter()
	logging_level = logging.INFO
	logging.basicConfig(level=logging_level, format="%(levelname)s: %(message)s")
	module_logger.setLevel(logging.INFO)

	input_contains_explicit_run_number = has_explicit_run_number(ROOT_JOBRUN_URL)
	resolved_pipeline_execution_url = resolve_job_url_to_latest_jobrun_url(ROOT_JOBRUN_URL)
	if not input_contains_explicit_run_number:
		module_logger.info(f"Resolved latest pipeline run: {resolved_pipeline_execution_url}")
	module_logger.info(f"Pipeline: {resolved_pipeline_execution_url}")
	output_directory = Path(aggregated_reports_directory).expanduser().resolve()
	output_directory.mkdir(parents=True, exist_ok=True)
	output_result_path = make_output_csv_path(resolved_pipeline_execution_url, output_directory)
	try:
		export_multi_jobs_results(pipeline_execution_url=resolved_pipeline_execution_url, destination_csv_path=output_result_path,
		                          jobrun_url_list=None, retain_latest_execution_per_job=True)
	finally:
		elapsed_duration_seconds = time.perf_counter() - start_time
		module_logger.info(f"Total runtime: {render_elapsed_time(elapsed_duration_seconds)}")
