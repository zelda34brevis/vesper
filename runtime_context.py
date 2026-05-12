from __future__ import annotations

import os
from pathlib import Path

from settings import AGGREGATED_REPORTS_DIRECTORY, BACKTRACE_OUTPUT_DIRECTORY, TEST_LOGS_OUTPUT_DIRECTORY

JENKINS_USERNAME = os.getenv("JENKINS_API_USER")
JENKINS_API_TOKEN = os.getenv("JENKINS_API_TOKEN")

aggregated_reports_directory = Path(AGGREGATED_REPORTS_DIRECTORY).expanduser()
backtrace_output_directory = Path(BACKTRACE_OUTPUT_DIRECTORY).expanduser()
test_logs_output_directory = Path(TEST_LOGS_OUTPUT_DIRECTORY).expanduser()

TEST_RESULTS_PROCESSOR_DIRECTORY = Path(__file__).resolve().parent
