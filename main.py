from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# flake8: noqa: E402
# pylint: disable=wrong-import-position,import-error

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
	sys.path.insert(0, str(SCRIPT_DIRECTORY))

from runtime_context import aggregated_reports_directory
from settings import ROOT_JOBRUN_URL
from utils.time_formatting import render_elapsed_time
from utils.pipeline_csv_export import export_pipeline_results as _export_pipeline_results
from utils.pipeline_report_paths import make_output_csv_path, make_pipeline_cache_scope_dirname

from utils.single_jobrun_results import allure_reports_directory

module_logger = logging.getLogger(__name__)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(
	logging.Formatter('%(asctime)s.%(msecs)03d %(name)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
module_logger.addHandler(stdout_handler)
module_logger.propagate = False


def _create_arg_parser() -> argparse.ArgumentParser:
	cli_parser = argparse.ArgumentParser(description="Gather Jenkins child-run test results and save them as CSV")
	cli_parser.add_argument(
		"--root-run-url",
		default=ROOT_JOBRUN_URL,
		help="Top-level multijob run URL used to locate downstream job runs",
	)
	cli_parser.add_argument(
		"--output-dir",
		default=str(aggregated_reports_directory),
		help="Directory used to store aggregated CSV reports",
	)
	cli_parser.add_argument(
		"--keep-all-executions",
		action="store_true",
		help=(
			"Turn off the default deduplication and preserve every execution in the CSV "
			"(including retries or reruns of the same test within one job run)"
		),
	)
	return cli_parser


def export_pipeline_results(
	pipeline_execution_url,
	destination_csv_path,
	max_traversal_depth = 25,
	retain_latest_execution_per_job = True,
) -> int:
	"""Gather child runs for a pipeline execution and write all test results to CSV."""
	return _export_pipeline_results(
		pipeline_execution_url=pipeline_execution_url,
		destination_csv_path=destination_csv_path,
		max_traversal_depth=max_traversal_depth,
		retain_latest_execution_per_job=retain_latest_execution_per_job,
	)


if __name__ == "__main__":
	started_monotonic_time = time.perf_counter()
	cli_args = _create_arg_parser().parse_args()
	logging_level = logging.INFO
	logging.basicConfig(level=logging_level, format="%(levelname)s: %(message)s")
	module_logger.setLevel(logging.INFO)

	module_logger.info(f"Pipeline: {cli_args.root_run_url}")
	output_directory = Path(cli_args.output_dir).expanduser().resolve()
	output_directory.mkdir(parents=True, exist_ok=True)
	cache_scope_directory = (allure_reports_directory / make_pipeline_cache_scope_dirname(cli_args.root_run_url)).resolve()
	output_result_path = make_output_csv_path(cli_args.root_run_url, output_directory)
	try:
		export_pipeline_results(
			pipeline_execution_url=cli_args.root_run_url,
			destination_csv_path=output_result_path,
			retain_latest_execution_per_job=not cli_args.keep_all_executions,
		)
	finally:
		elapsed_duration_seconds = time.perf_counter() - started_monotonic_time
		module_logger.info(f"Total runtime: {render_elapsed_time(elapsed_duration_seconds)}")
