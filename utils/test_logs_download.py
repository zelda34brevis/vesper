from __future__ import annotations

import logging
import traceback
from pathlib import Path
from urllib.parse import quote, unquote

from runtime_context import JENKINS_API_TOKEN, JENKINS_USERNAME, test_logs_output_directory
from .filesystem_helpers import make_safe_component
from .jenkins_helpers import normalize_run_url, create_url_opener, download_url_to_file, parse_job_name_and_run_number, \
    fetch_url_json

module_logger = logging.getLogger(__name__)


def _standardize_test_id(test_id_text) -> tuple[str, str]:
    normalized_value = test_id_text.strip().upper()
    return normalized_value, normalized_value.replace("-", "_")


def _locate_matching_artifact_relative_path(artifact_items, test_id_text) -> str:
    dash_identifier_variant, underscore_identifier_variant = _standardize_test_id(test_id_text)
    zip_candidate_names: list[str] = []
    non_zip_artifact_count = 0

    for artifact_entry in artifact_items:
        artifact_filename = artifact_entry.get("fileName", "")
        artifact_relative_path = artifact_entry.get("relativePath", "")
        candidate_path = str(artifact_filename or artifact_relative_path)
        if not candidate_path:
            continue

        candidate_uppercase = candidate_path.upper()
        if not candidate_uppercase.endswith(".ZIP"):
            non_zip_artifact_count += 1
            continue

        zip_candidate_names.append(candidate_path)
        if dash_identifier_variant in candidate_uppercase or underscore_identifier_variant in candidate_uppercase:
            module_logger.debug(
                f"Matched test-logs artifact for test_id={test_id_text}: candidate={candidate_path}, relative_path={artifact_relative_path}"
            )
            return artifact_relative_path

    module_logger.debug(
        f"No matching test-logs artifact for test_id={test_id_text}. "
        f"zip_candidates={zip_candidate_names}, non_zip_artifacts={non_zip_artifact_count}"
    )

    raise FileNotFoundError(
        f"No matching ZIP artifact was found for {test_id_text}. "
        f"Expected a ZIP name containing {dash_identifier_variant} or {underscore_identifier_variant}"
    )


def _determine_run_scope_dir(job_run_url, logs_scope_name = None) -> Path:
    """Resolve the destination directory for downloaded test logs."""
    if logs_scope_name is not None and logs_scope_name.strip():
        safe_scope_label = unquote(logs_scope_name).replace("/", "_").strip() or "unknown_scope"
        return test_logs_output_directory / safe_scope_label

    normalized_run_url = normalize_run_url(job_run_url)
    job_display_name, job_run_number = parse_job_name_and_run_number(normalized_run_url)
    safe_job_label = make_safe_component(job_display_name, default_value="unknown_job", allow_dots=True)
    return test_logs_output_directory / f"{safe_job_label}-{job_run_number}"


def fetch_test_logs_artifact_for_test(
    job_run_url,
    test_id_text,
    logs_scope_name = None,
    request_timeout_seconds = 20,
    jenkins_username = None,
    jenkins_api_token = None,
) -> Path:
    """Download the test-log ZIP artifact for a test from a Jenkins job run."""
    jenkins_username = JENKINS_USERNAME if jenkins_username is None else jenkins_username
    jenkins_api_token = JENKINS_API_TOKEN if jenkins_api_token is None else jenkins_api_token

    normalized_run_url = normalize_run_url(job_run_url)
    http_opener = create_url_opener(jenkins_username=jenkins_username, jenkins_api_token=jenkins_api_token)
    run_scope_directory = _determine_run_scope_dir(job_run_url=normalized_run_url, logs_scope_name=logs_scope_name)

    try:
        api_tree = "artifacts[fileName,relativePath]"
        build_metadata = fetch_url_json(f"{normalized_run_url}api/json?tree={api_tree}", url_opener=http_opener, request_timeout_seconds=request_timeout_seconds)
    except Exception as error_details:
        module_logger.warning(f"Error: Unable to read Jenkins artifacts for {normalized_run_url}: {error_details}")
        module_logger.warning(f"Stack trace: {traceback.format_exc()}")
        raise RuntimeError(f"Unable to fetch Jenkins artifacts for {normalized_run_url}") from error_details

    artifact_entries = build_metadata.get("artifacts") or []
    try:
        artifact_relative_path = _locate_matching_artifact_relative_path(artifact_entries, test_id_text=test_id_text)
    except FileNotFoundError as error_details:
        module_logger.info(
            f"Test-logs artifact was not found for test_id={test_id_text} in {normalized_run_url}: {error_details}"
        )
        raise
    except Exception as error_details:
        module_logger.warning(f"Error: Unable to find a matching artifact for test_id={test_id_text} in {normalized_run_url}: {error_details}")
        module_logger.warning(f"Stack trace: {traceback.format_exc()}")
        raise

    local_artifact_path = (run_scope_directory / Path(artifact_relative_path).name).resolve()
    if local_artifact_path.exists():
        module_logger.info(f"Test-logs artifact already exists, skipping download: {local_artifact_path}")
        return local_artifact_path

    artifact_download_url = f"{normalized_run_url}artifact/{quote(artifact_relative_path, safe='/')}"
    try:
        download_url_to_file(source_url=artifact_download_url, output_file_path=local_artifact_path, url_opener=http_opener, request_timeout_seconds=request_timeout_seconds)
    except Exception as error_details:
        module_logger.warning(f"Error: Unable to download the artifact from {artifact_download_url}: {error_details}")
        module_logger.warning(f"Stack trace: {traceback.format_exc()}")
        raise RuntimeError(f"Unable to download the artifact from {artifact_download_url}") from error_details

    return local_artifact_path
