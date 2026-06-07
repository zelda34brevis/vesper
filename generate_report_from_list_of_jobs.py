# flake8: noqa: E402
# pylint: disable=wrong-import-position,import-error

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from utils.time_formatting import get_timestamp_iso

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
	sys.path.insert(0, str(SCRIPT_DIRECTORY))

from runtime_context import aggregated_reports_directory
from settings import JOB_URLS
from utils.time_formatting import render_elapsed_time
from utils.downstream_jobruns import discover_jobrun_urls_from_job_url_list
from utils.pipeline_csv_export import export_multi_jobs_results

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

	JOB_URLS = [url for url in JOB_URLS]
	module_logger.info("Jobs list start ---")
	for url in JOB_URLS:
		if not url.endswith('/'):
			url = url + '/'
		module_logger.info(url.split('/')[-2])
	module_logger.info("Jobs list end ---")

	output_directory = Path(aggregated_reports_directory).expanduser().resolve()
	output_directory.mkdir(parents=True, exist_ok=True)
	output_result_path = Path(output_directory, f'{get_timestamp_iso()}_report.csv')

	try:
		jobrun_urls = discover_jobrun_urls_from_job_url_list(JOB_URLS)
		module_logger.info(f"Resolved latest jobrun URLs. Number of jobs processed: {len(jobrun_urls)}")

		export_multi_jobs_results(pipeline_execution_url=None, destination_csv_path=output_result_path,
		                          jobrun_url_list=jobrun_urls, retain_latest_execution_per_job=True)
	finally:
		elapsed_duration_seconds = time.perf_counter() - start_time
		module_logger.info(f"Total runtime: {render_elapsed_time(elapsed_duration_seconds)}")
