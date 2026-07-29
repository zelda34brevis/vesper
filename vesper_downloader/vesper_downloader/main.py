from __future__ import annotations

import json
import logging
import os
import traceback
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener as urllib_build_opener, urlopen

from vesper_core import ArtifactRecord, FailedUrlRecord, Manifest, RunRecord, make_safe_component, manifest_path_for_scope

if __package__ in {None, ""}:
    from logging_utils import configure_logging
else:
    from .logging_utils import configure_logging

LOGGER = logging.getLogger(__name__)
DOWNLOAD_CHUNK_SIZE_BYTES = 1024 * 1024
DEFAULT_FILE_DOWNLOAD_ATTEMPTS = 6
MAX_UNEXPECTED_FULL_RESPONSE_RESETS = 1


def _format_byte_count(byte_count: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(byte_count)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{byte_count} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]} ({byte_count} bytes)"


@dataclass
class JenkinsConfig:
    base_url: str | None = None
    username_env: str = "JENKINS_API_USER"
    token_env: str = "JENKINS_API_TOKEN"
    request_timeout_seconds: int = 20

    @property
    def username(self) -> str | None:
        return os.getenv(self.username_env)

    @property
    def token(self) -> str | None:
        return os.getenv(self.token_env)


@dataclass
class InputConfig:
    pipeline_url: str | None = None
    job_urls: list[str] = field(default_factory=list)
    job_urls_skip: list[str] = field(default_factory=list)
    build_selector: str = "lastBuild"
    max_traversal_depth: int = 25
    fallback_to_root_run_when_no_downstream: bool = True


@dataclass
class OutputConfig:
    output_root: str = "~/vesper_downloader_output"
    scope_name: str | None = None
    download_only_zip: bool = True


@dataclass
class DownloaderConfig:
    jenkins: JenkinsConfig = field(default_factory=JenkinsConfig)
    input: InputConfig = field(default_factory=InputConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


ConfigSection = TypeVar("ConfigSection", JenkinsConfig, InputConfig, OutputConfig)


@dataclass
class ResolvedRunRequest:
    run_url: str
    requested_by_urls: list[str] = field(default_factory=list)


def load_config(config_path: Path) -> DownloaderConfig:
    raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    return DownloaderConfig(
        jenkins=_load_config_section(raw_payload.get("jenkins", {}), JenkinsConfig, "jenkins"),
        input=_load_config_section(raw_payload.get("input", {}), InputConfig, "input"),
        output=_load_config_section(raw_payload.get("output", {}), OutputConfig, "output"),
    )


def _load_config_section(raw_section: Any, config_type: type[ConfigSection], section_name: str) -> ConfigSection:
    if raw_section is None:
        return config_type()
    if not isinstance(raw_section, Mapping):
        raise ValueError(f"{section_name} config section must be a JSON object")
    allowed_field_names = {config_field.name for config_field in fields(config_type)}
    filtered_section = {key: value for key, value in raw_section.items() if key in allowed_field_names}
    unexpected_field_names = sorted(set(raw_section) - allowed_field_names)
    if unexpected_field_names:
        LOGGER.warning(
            "Ignoring unexpected %s config field(s): %s",
            section_name,
            ", ".join(unexpected_field_names),
        )
    return config_type(**filtered_section)


def validate_config(config: DownloaderConfig) -> None:
    input_config = config.input
    if bool(input_config.pipeline_url) == bool(input_config.job_urls):
        raise ValueError("Provide exactly one input source: pipeline_url or job_urls")
    skipped_job_urls = normalize_job_url_list(input_config.job_urls_skip, config_field_name="input.job_urls_skip")
    if input_config.build_selector not in {"lastBuild", "lastCompletedBuild"}:
        raise ValueError("input.build_selector must be 'lastBuild' or 'lastCompletedBuild'")
    if input_config.max_traversal_depth < 1:
        raise ValueError("input.max_traversal_depth must be at least 1")
    if input_config.pipeline_url and is_job_url_skipped(input_config.pipeline_url, skipped_job_urls):
        raise ValueError("input.pipeline_url points to a job that is skipped by input.job_urls_skip")


def get_timestamp_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def normalize_run_url(raw_url: str) -> str:
    sanitized_value = str(raw_url).strip()
    url_parts = urlsplit(sanitized_value)
    normalized_path = url_parts.path if url_parts.path.endswith("/") else f"{url_parts.path}/"
    return urlunsplit((url_parts.scheme, url_parts.netloc, normalized_path, "", ""))


def _extract_job_display_name(raw_url: str) -> str | None:
    path_segments = [path_part for path_part in urlsplit(str(raw_url).strip()).path.split("/") if path_part]
    job_name_segments: list[str] = []
    segment_index = 0
    while segment_index < len(path_segments) - 1:
        if path_segments[segment_index] != "job":
            segment_index += 1
            continue
        name_segment = unquote(path_segments[segment_index + 1]).strip()
        if name_segment:
            job_name_segments.append(name_segment)
        segment_index += 2
    if not job_name_segments:
        return None
    return "/".join(job_name_segments)


def normalize_job_url(raw_url: str) -> str:
    sanitized_value = str(raw_url).strip()
    if not sanitized_value:
        raise ValueError("Job URL value must be a non-empty string")
    url_parts = urlsplit(sanitized_value)
    job_display_name = _extract_job_display_name(sanitized_value)
    if not job_display_name:
        raise ValueError(f"Could not extract a Jenkins job path from URL: {raw_url}")
    normalized_path = f"/{compose_job_path(job_display_name)}/"
    return urlunsplit((url_parts.scheme, url_parts.netloc, normalized_path, "", ""))


def normalize_job_url_list(raw_urls: list[str], config_field_name: str) -> set[str]:
    normalized_job_urls: set[str] = set()
    for raw_url in raw_urls:
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ValueError(f"{config_field_name} must contain only non-empty string URLs")
        normalized_job_urls.add(normalize_job_url(raw_url))
    return normalized_job_urls


def is_job_url_skipped(candidate_url: str, skipped_job_urls: set[str]) -> bool:
    return normalize_job_url(candidate_url) in skipped_job_urls


def has_explicit_run_number(raw_url: str) -> bool:
    sanitized_value = str(raw_url).strip()
    path_segments = [path_part for path_part in urlsplit(sanitized_value).path.split("/") if path_part]
    return bool(path_segments and path_segments[-1].isdigit())


def compose_job_path(display_job_name: str) -> str:
    prepared_job_name = str(display_job_name).replace("»", "/")
    path_segments = [name_segment.strip() for name_segment in prepared_job_name.split("/") if name_segment.strip()]
    encoded_segments = [quote(name_segment, safe="") for name_segment in path_segments]
    return "/".join(f"job/{name_segment}" for name_segment in encoded_segments)


def parse_job_name_and_run_number(run_base_url: str) -> tuple[str, str]:
    url_parts = [path_part for path_part in urlsplit(run_base_url).path.split("/") if path_part]
    job_run_number = next((path_part for path_part in reversed(url_parts) if path_part.isdigit()), "unknown_run")
    run_number_index = next(
        (entry_index for entry_index in range(len(url_parts) - 1, -1, -1) if url_parts[entry_index].isdigit()),
        None,
    )

    job_name_segments: list[str] = []
    search_upper_bound = run_number_index if run_number_index is not None else len(url_parts)
    for entry_index in range(search_upper_bound - 1):
        if url_parts[entry_index] != "job":
            continue
        name_segment = unquote(url_parts[entry_index + 1]).strip()
        if name_segment:
            job_name_segments.append(name_segment)

    job_display_name = "/".join(job_name_segments) if job_name_segments else "unknown_job"
    return job_display_name, job_run_number


def build_run_directory_name(run_url: str) -> str:
    job_name, run_number = parse_job_name_and_run_number(run_url)
    safe_job_name = make_safe_component(job_name.replace("/", "_"), default_value="unknown_job", allow_dots=True)
    safe_run_number = make_safe_component(run_number, default_value="unknown_run", allow_dots=True)
    return f"{safe_job_name}-{safe_run_number}"


def create_url_opener(jenkins_username: str | None, jenkins_api_token: str | None):
    if not jenkins_username or not jenkins_api_token:
        return None

    password_manager = HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, uri="*", user=jenkins_username, passwd=jenkins_api_token)
    return urllib_build_opener(HTTPBasicAuthHandler(password_manager))


def _open_url_response(
    target_url: str,
    url_opener,
    request_timeout_seconds: int,
    request_headers: Mapping[str, str] | None = None,
):
    prepared_request_headers: dict[str, str] = dict(request_headers or {})
    url_request = Request(target_url, headers=prepared_request_headers)
    if url_opener is not None:
        return url_opener.open(url_request, timeout=request_timeout_seconds)
    return urlopen(url_request, timeout=request_timeout_seconds)


def fetch_url_bytes(target_url: str, url_opener, request_timeout_seconds: int) -> bytes:
    http_response = _open_url_response(
        target_url=target_url,
        url_opener=url_opener,
        request_timeout_seconds=request_timeout_seconds,
    )
    with http_response:
        return http_response.read()


def fetch_url_json(target_url: str, url_opener, request_timeout_seconds: int) -> dict[str, Any]:
    payload_bytes = fetch_url_bytes(target_url=target_url, url_opener=url_opener, request_timeout_seconds=request_timeout_seconds)
    return json.loads(payload_bytes.decode("utf-8"))


def _get_response_status_code(http_response) -> int | None:
    if hasattr(http_response, "status"):
        return http_response.status
    if hasattr(http_response, "getcode"):
        return http_response.getcode()
    return None


def _extract_expected_total_bytes_from_headers(response_headers: Mapping[str, str], range_start_byte: int) -> int:
    content_range_header = str(response_headers.get("Content-Range") or "").strip()
    if content_range_header:
        _, _, total_size_text = content_range_header.partition("/")
        if total_size_text.isdigit():
            return int(total_size_text)

    content_length_header = str(response_headers.get("Content-Length") or "").strip()
    if content_length_header.isdigit():
        content_length_value = int(content_length_header)
        return range_start_byte + content_length_value if range_start_byte else content_length_value

    raise RuntimeError("Unable to determine the remote file size from response headers")


def _stream_response_into_file(http_response, destination_path: Path, chunk_size_bytes: int = DOWNLOAD_CHUNK_SIZE_BYTES) -> int:
    bytes_written = 0
    with destination_path.open("ab") as output_stream:
        while True:
            try:
                payload_chunk = http_response.read(chunk_size_bytes)
            except IncompleteRead as error_details:
                partial_payload = error_details.partial or b""
                if partial_payload:
                    output_stream.write(partial_payload)
                    bytes_written += len(partial_payload)
                raise

            if not payload_chunk:
                break

            output_stream.write(payload_chunk)
            bytes_written += len(payload_chunk)

    return bytes_written


def _make_range_request_headers(resume_from_byte: int) -> dict[str, str]:
    if resume_from_byte <= 0:
        return {}
    return {"Range": f"bytes={resume_from_byte}-"}


def _recover_completed_partial_file_from_http_416(http_error, partial_output_path: Path, output_file_path: Path, source_url: str) -> bool:
    if http_error.code != 416 or not partial_output_path.exists():
        return False

    try:
        expected_total_bytes = _extract_expected_total_bytes_from_headers(http_error.headers or {}, range_start_byte=0)
        downloaded_file_size = partial_output_path.stat().st_size
        if downloaded_file_size != expected_total_bytes:
            return False

        os.replace(partial_output_path, output_file_path)
        LOGGER.info("Recovered completed partial download after HTTP 416: %s -> %s", source_url, output_file_path)
        return True
    except Exception as error_details:
        LOGGER.warning("Error: Could not recover completed partial download after HTTP 416 for %s: %s", source_url, error_details)
        LOGGER.warning("Traceback: %s", traceback.format_exc())
        return False


def _should_restart_from_scratch_after_unexpected_resume_response(
    response_status_code: int | None,
    resumed_from_byte: int,
    unexpected_full_response_reset_count: int,
    partial_output_path: Path,
    source_url: str,
) -> bool:
    if resumed_from_byte <= 0 or response_status_code == 206:
        return False

    if unexpected_full_response_reset_count >= MAX_UNEXPECTED_FULL_RESPONSE_RESETS:
        raise RuntimeError(
            f"Resume request for {source_url} returned HTTP {response_status_code} after a prior reset; keeping {partial_output_path}"
        )

    LOGGER.warning(
        "Resume request for %s returned HTTP %s instead of 206; deleting %s and retrying from scratch",
        source_url,
        response_status_code,
        partial_output_path,
    )
    partial_output_path.unlink(missing_ok=True)
    return True


def _finalize_download_if_complete(partial_output_path: Path, output_file_path: Path, expected_total_bytes: int, source_url: str) -> bool:
    downloaded_file_size = partial_output_path.stat().st_size if partial_output_path.exists() else 0
    if downloaded_file_size == expected_total_bytes:
        os.replace(partial_output_path, output_file_path)
        return True

    if downloaded_file_size > expected_total_bytes:
        raise RuntimeError(
            f"Downloaded file size mismatch for {source_url}: got {downloaded_file_size} bytes, expected {expected_total_bytes}"
        )

    raise RuntimeError(
        f"Downloaded file is still incomplete for {source_url}: got {downloaded_file_size} bytes, expected {expected_total_bytes}"
    )


def download_url_to_file(
    source_url: str,
    output_file_path: Path,
    url_opener,
    request_timeout_seconds: int,
    max_file_download_attempts: int = DEFAULT_FILE_DOWNLOAD_ATTEMPTS,
) -> Path:
    output_file_path = Path(output_file_path)
    output_file_path.parent.mkdir(parents=True, exist_ok=True)
    if output_file_path.exists():
        LOGGER.info("Reusing existing downloaded file: %s -> %s", source_url, output_file_path)
        return output_file_path

    partial_output_path = output_file_path.parent / f"{output_file_path.name}.part"
    last_error: Exception | None = None
    unexpected_full_response_reset_count = 0

    for attempt_number in range(1, max_file_download_attempts + 1):
        resumed_from_byte = partial_output_path.stat().st_size if partial_output_path.exists() else 0
        request_headers = _make_range_request_headers(resume_from_byte=resumed_from_byte)

        try:
            http_response = _open_url_response(
                target_url=source_url,
                url_opener=url_opener,
                request_timeout_seconds=request_timeout_seconds,
                request_headers=request_headers,
            )
        except HTTPError as error_details:
            if _recover_completed_partial_file_from_http_416(
                http_error=error_details,
                partial_output_path=partial_output_path,
                output_file_path=output_file_path,
                source_url=source_url,
            ):
                return output_file_path
            LOGGER.warning(
                "Error: Could not open %s for download on attempt %s/%s: %s",
                source_url,
                attempt_number,
                max_file_download_attempts,
                error_details,
            )
            LOGGER.warning("Traceback: %s", traceback.format_exc())
            raise
        except Exception as error_details:
            last_error = error_details
            LOGGER.warning(
                "Error: Download open failed for %s on attempt %s/%s: %s",
                source_url,
                attempt_number,
                max_file_download_attempts,
                error_details,
            )
            LOGGER.warning("Traceback: %s", traceback.format_exc())
            if attempt_number == max_file_download_attempts:
                break
            continue

        with http_response:
            response_status_code = _get_response_status_code(http_response)
            try:
                should_restart_from_scratch = _should_restart_from_scratch_after_unexpected_resume_response(
                    response_status_code=response_status_code,
                    resumed_from_byte=resumed_from_byte,
                    unexpected_full_response_reset_count=unexpected_full_response_reset_count,
                    partial_output_path=partial_output_path,
                    source_url=source_url,
                )
            except RuntimeError as error_details:
                last_error = error_details
                LOGGER.warning("Error: %s", last_error)
                break

            if should_restart_from_scratch:
                unexpected_full_response_reset_count += 1
                continue

            try:
                expected_total_bytes = _extract_expected_total_bytes_from_headers(http_response.headers, range_start_byte=resumed_from_byte)
            except Exception as error_details:
                last_error = error_details
                LOGGER.warning(
                    "Error: Could not resolve file size for %s on attempt %s/%s: %s",
                    source_url,
                    attempt_number,
                    max_file_download_attempts,
                    error_details,
                )
                LOGGER.warning("Traceback: %s", traceback.format_exc())
                break

            LOGGER.info(
                "Downloading file (attempt %s/%s): %s -> %s [total=%s%s]",
                attempt_number,
                max_file_download_attempts,
                source_url,
                output_file_path,
                _format_byte_count(expected_total_bytes),
                f", resume={_format_byte_count(resumed_from_byte)}" if resumed_from_byte else "",
            )

            try:
                _stream_response_into_file(http_response=http_response, destination_path=partial_output_path)
            except IncompleteRead as error_details:
                last_error = error_details
                LOGGER.warning(
                    "Error: Incomplete download for %s on attempt %s/%s: %s",
                    source_url,
                    attempt_number,
                    max_file_download_attempts,
                    error_details,
                )
                LOGGER.warning("Traceback: %s", traceback.format_exc())
                if attempt_number == max_file_download_attempts:
                    break
                continue
            except Exception as error_details:
                last_error = error_details
                LOGGER.warning(
                    "Error: Download stream failed for %s on attempt %s/%s: %s",
                    source_url,
                    attempt_number,
                    max_file_download_attempts,
                    error_details,
                )
                LOGGER.warning("Traceback: %s", traceback.format_exc())
                if attempt_number == max_file_download_attempts:
                    break
                continue

        try:
            if _finalize_download_if_complete(
                partial_output_path=partial_output_path,
                output_file_path=output_file_path,
                expected_total_bytes=expected_total_bytes,
                source_url=source_url,
            ):
                LOGGER.info(
                    "Finished download: %s -> %s [%s]",
                    source_url,
                    output_file_path,
                    _format_byte_count(expected_total_bytes),
                )
                return output_file_path
        except RuntimeError as error_details:
            last_error = error_details
            LOGGER.warning("Error: %s", last_error)

        if attempt_number == max_file_download_attempts:
            break

    if last_error is None:
        last_error = RuntimeError(f"Unable to download the file from {source_url}")
    raise RuntimeError(f"Unable to download the file from {source_url}") from last_error


def create_job_payload_fetcher(config: DownloaderConfig) -> Callable[[str], dict[str, Any]]:
    url_opener = create_url_opener(config.jenkins.username, config.jenkins.token)
    timeout_seconds = config.jenkins.request_timeout_seconds

    def fetch_job_payload(normalized_job_url: str) -> dict[str, Any]:
        api_tree = "url,fullName,name,lastCompletedBuild[url,number],lastBuild[url,number]"
        api_endpoint_url = f"{normalize_run_url(normalized_job_url)}api/json?tree={api_tree}"
        return fetch_url_json(target_url=api_endpoint_url, url_opener=url_opener, request_timeout_seconds=timeout_seconds)

    return fetch_job_payload


def create_build_payload_fetcher(config: DownloaderConfig) -> Callable[[str], dict[str, Any]]:
    url_opener = create_url_opener(config.jenkins.username, config.jenkins.token)
    timeout_seconds = config.jenkins.request_timeout_seconds

    def fetch_build_payload(normalized_build_run_url: str) -> dict[str, Any]:
        api_tree = (
            "url,subBuilds[url,jobName,jobUrl,buildNumber],"
            "runs[url,jobName,jobUrl,buildNumber],"
            "builds[url,jobName,jobUrl,buildNumber],"
            "actions[downstreamBuilds[url,jobName,jobUrl,buildNumber],"
            "subBuilds[url,jobName,jobUrl,buildNumber],"
            "builds[url,jobName,jobUrl,buildNumber]]"
        )
        api_endpoint_url = f"{normalize_run_url(normalized_build_run_url)}api/json?tree={api_tree}"
        return fetch_url_json(target_url=api_endpoint_url, url_opener=url_opener, request_timeout_seconds=timeout_seconds)

    return fetch_build_payload


def create_artifact_metadata_fetcher(config: DownloaderConfig) -> Callable[[str], dict[str, Any]]:
    url_opener = create_url_opener(config.jenkins.username, config.jenkins.token)
    timeout_seconds = config.jenkins.request_timeout_seconds

    def fetch_artifact_metadata(normalized_run_url: str) -> dict[str, Any]:
        api_tree = "artifacts[fileName,relativePath]"
        api_endpoint_url = f"{normalize_run_url(normalized_run_url)}api/json?tree={api_tree}"
        return fetch_url_json(target_url=api_endpoint_url, url_opener=url_opener, request_timeout_seconds=timeout_seconds)

    return fetch_artifact_metadata


def create_artifact_downloader(config: DownloaderConfig) -> Callable[[str, Path], Path]:
    url_opener = create_url_opener(config.jenkins.username, config.jenkins.token)
    timeout_seconds = config.jenkins.request_timeout_seconds

    def perform_download(source_url: str, destination_path: Path) -> Path:
        return download_url_to_file(
            source_url=source_url,
            output_file_path=destination_path,
            url_opener=url_opener,
            request_timeout_seconds=timeout_seconds,
        )

    return perform_download


def _resolve_child_run_url(item_payload: dict[str, Any], parent_base_url: str) -> str | None:
    direct_run_url = item_payload.get("url")
    if isinstance(direct_run_url, str) and direct_run_url.strip():
        if "://" in direct_run_url:
            return normalize_run_url(direct_run_url)
        root_url_parts = urlsplit(parent_base_url)
        relative_path = direct_run_url if direct_run_url.startswith("/") else f"/{direct_run_url}"
        return normalize_run_url(urlunsplit((root_url_parts.scheme, root_url_parts.netloc, relative_path, "", "")))

    resolved_build_number = item_payload.get("buildNumber", item_payload.get("number"))
    if isinstance(resolved_build_number, str) and resolved_build_number.isdigit():
        resolved_build_number = int(resolved_build_number)
    if not isinstance(resolved_build_number, int):
        return None

    job_base_url = item_payload.get("jobUrl")
    if isinstance(job_base_url, str) and job_base_url.strip():
        return normalize_run_url(f"{normalize_run_url(job_base_url)}{resolved_build_number}/")

    job_display_name = item_payload.get("jobName") or item_payload.get("name")
    if isinstance(job_display_name, str) and job_display_name.strip():
        root_url_parts = urlsplit(parent_base_url)
        job_route_path = compose_job_path(job_display_name)
        return normalize_run_url(
            urlunsplit((root_url_parts.scheme, root_url_parts.netloc, f"/{job_route_path}/{resolved_build_number}/", "", ""))
        )

    return None


def resolve_job_url_to_run_url(
    job_or_jobrun_url: str,
    build_selector: str,
    job_payload_fetcher: Callable[[str], dict[str, Any]],
) -> str:
    normalized_url = normalize_run_url(job_or_jobrun_url)
    if has_explicit_run_number(normalized_url):
        return normalized_url

    job_payload = job_payload_fetcher(normalized_url)
    latest_run_payload = job_payload.get(build_selector)
    if not isinstance(latest_run_payload, dict):
        raise RuntimeError(f"Could not resolve {build_selector} for {normalized_url}")

    latest_jobrun_url = _resolve_child_run_url(latest_run_payload, parent_base_url=normalized_url)
    if not latest_jobrun_url or not has_explicit_run_number(latest_jobrun_url):
        raise RuntimeError(f"Could not resolve a numeric run URL for {normalized_url}")
    return latest_jobrun_url


def _collect_downstream_urls(build_payload: dict[str, Any], parent_base_url: str) -> list[str]:
    collected_downstream_urls: list[str] = []

    def append_urls_from_list(candidate_entries: object) -> None:
        if not isinstance(candidate_entries, list):
            return
        for item_payload in candidate_entries:
            if not isinstance(item_payload, dict):
                continue
            resolved_child_run_url = _resolve_child_run_url(item_payload, parent_base_url=parent_base_url)
            if resolved_child_run_url:
                collected_downstream_urls.append(resolved_child_run_url)

    append_urls_from_list(build_payload.get("runs"))
    append_urls_from_list(build_payload.get("builds"))
    append_urls_from_list(build_payload.get("subBuilds"))

    for action_entry in build_payload.get("actions") or []:
        if not isinstance(action_entry, dict):
            continue
        append_urls_from_list(action_entry.get("downstreamBuilds"))
        append_urls_from_list(action_entry.get("subBuilds"))
        append_urls_from_list(action_entry.get("builds"))

    return collected_downstream_urls


def discover_downstream_jobrun_urls(
    root_run_url: str,
    build_payload_fetcher: Callable[[str], dict[str, Any]],
    max_traversal_depth: int,
) -> list[str]:
    from collections import deque

    pending_queue: deque[tuple[str, int]] = deque([(normalize_run_url(root_run_url), 0)])
    visited_run_urls: set[str] = set()
    discovered_run_urls: list[str] = []
    seen_run_urls: set[str] = set()

    while pending_queue:
        current_run_url, current_depth = pending_queue.popleft()
        if current_run_url in visited_run_urls:
            continue
        visited_run_urls.add(current_run_url)

        try:
            build_payload = build_payload_fetcher(current_run_url)
        except HTTPError as error_details:
            if error_details.code == 404:
                LOGGER.warning("Skipping missing Jenkins run %s (HTTP 404)", current_run_url)
                continue
            LOGGER.warning("Problem while fetching Jenkins run data for %s: %s", current_run_url, error_details)
            continue
        except Exception as error_details:
            LOGGER.warning("Problem while fetching Jenkins run data for %s: %s", current_run_url, error_details)
            continue

        for child_run_url in _collect_downstream_urls(build_payload, parent_base_url=current_run_url):
            if child_run_url not in seen_run_urls:
                discovered_run_urls.append(child_run_url)
                seen_run_urls.add(child_run_url)

            if current_depth + 1 < max_traversal_depth and child_run_url not in visited_run_urls:
                pending_queue.append((child_run_url, current_depth + 1))

    return discovered_run_urls


def classify_artifact(file_name: str, source_relative_path: str) -> str:
    candidate_path = f"{source_relative_path}/{file_name}".lower()
    if candidate_path.endswith("allure-report.zip"):
        return "allure-report"
    return "zip"


def should_download_artifact(file_name: str, source_relative_path: str, download_only_zip: bool) -> bool:
    if not download_only_zip:
        return True
    candidate_value = f"{source_relative_path}/{file_name}".lower()
    return candidate_value.endswith(".zip")


def select_scope_name(config: DownloaderConfig, source_mode: str, resolved_root_run_url: str | None) -> str:
    if config.output.scope_name:
        return make_safe_component(config.output.scope_name, default_value="download_scope")
    if source_mode == "pipeline_url" and resolved_root_run_url:
        return build_run_directory_name(resolved_root_run_url)
    return f"job-batch-{get_timestamp_iso()}"


def _resolve_requested_runs(
    config: DownloaderConfig,
    job_payload_fetcher: Callable[[str], dict[str, Any]],
    build_payload_fetcher: Callable[[str], dict[str, Any]],
) -> tuple[str, list[str], str | None, list[ResolvedRunRequest], list[FailedUrlRecord]]:
    input_config = config.input
    failed_urls: list[FailedUrlRecord] = []
    skipped_job_urls = normalize_job_url_list(input_config.job_urls_skip, config_field_name="input.job_urls_skip")

    if input_config.pipeline_url:
        source_mode = "pipeline_url"
        requested_urls = [input_config.pipeline_url]
        resolved_root_run_url = resolve_job_url_to_run_url(
            input_config.pipeline_url,
            build_selector=input_config.build_selector,
            job_payload_fetcher=job_payload_fetcher,
        )
        downstream_jobrun_urls = discover_downstream_jobrun_urls(
            root_run_url=resolved_root_run_url,
            build_payload_fetcher=build_payload_fetcher,
            max_traversal_depth=input_config.max_traversal_depth,
        )
        if not downstream_jobrun_urls and input_config.fallback_to_root_run_when_no_downstream:
            downstream_jobrun_urls = [resolved_root_run_url]
        resolved_runs = [
            ResolvedRunRequest(run_url=run_url, requested_by_urls=[input_config.pipeline_url])
            for run_url in downstream_jobrun_urls
            if not is_job_url_skipped(run_url, skipped_job_urls)
        ]
        if not resolved_runs:
            raise RuntimeError("No Jenkins jobs remain to process after applying input.job_urls_skip")
        return source_mode, requested_urls, resolved_root_run_url, resolved_runs, failed_urls

    source_mode = "job_url_list"
    requested_urls = list(input_config.job_urls)
    resolved_root_run_url = None
    runs_by_url: dict[str, ResolvedRunRequest] = {}
    for raw_job_url in input_config.job_urls:
        if not isinstance(raw_job_url, str) or not raw_job_url.strip():
            failed_urls.append(FailedUrlRecord(url=str(raw_job_url), error="Invalid empty URL value"))
            continue
        if is_job_url_skipped(raw_job_url, skipped_job_urls):
            continue
        try:
            resolved_run_url = resolve_job_url_to_run_url(
                raw_job_url,
                build_selector=input_config.build_selector,
                job_payload_fetcher=job_payload_fetcher,
            )
        except Exception as error_details:
            failed_urls.append(FailedUrlRecord(url=raw_job_url, error=str(error_details)))
            LOGGER.warning("Problem while resolving Jenkins job URL %s: %s", raw_job_url, error_details)
            LOGGER.warning("Traceback: %s", traceback.format_exc())
            continue

        if is_job_url_skipped(resolved_run_url, skipped_job_urls):
            continue

        if resolved_run_url not in runs_by_url:
            runs_by_url[resolved_run_url] = ResolvedRunRequest(run_url=resolved_run_url, requested_by_urls=[])
        runs_by_url[resolved_run_url].requested_by_urls.append(raw_job_url)

    resolved_runs = list(runs_by_url.values())
    if not resolved_runs:
        raise RuntimeError("No Jenkins jobs remain to process after applying input.job_urls_skip")
    return source_mode, requested_urls, resolved_root_run_url, resolved_runs, failed_urls


def _download_run_artifacts(
    resolved_run: ResolvedRunRequest,
    scope_directory: Path,
    config: DownloaderConfig,
    artifact_metadata_fetcher: Callable[[str], dict[str, Any]],
    artifact_downloader: Callable[[str, Path], Path],
) -> RunRecord:
    normalized_run_url = normalize_run_url(resolved_run.run_url)
    job_name, run_number = parse_job_name_and_run_number(normalized_run_url)
    run_directory_name = build_run_directory_name(normalized_run_url)
    run_directory = scope_directory / run_directory_name
    run_directory.mkdir(parents=True, exist_ok=True)

    build_metadata = artifact_metadata_fetcher(normalized_run_url)
    artifact_entries = build_metadata.get("artifacts") or []
    artifact_records: list[ArtifactRecord] = []

    for artifact_entry in artifact_entries:
        artifact_file_name = str(artifact_entry.get("fileName") or "").strip()
        artifact_source_relative_path = str(artifact_entry.get("relativePath") or "").strip()
        if not artifact_file_name and not artifact_source_relative_path:
            continue
        if not should_download_artifact(artifact_file_name, artifact_source_relative_path, config.output.download_only_zip):
            continue

        artifact_relative_path = artifact_source_relative_path or artifact_file_name
        destination_path = (run_directory / Path(artifact_relative_path)).resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        download_url = f"{normalized_run_url}artifact/{quote(artifact_relative_path, safe='/')}"
        artifact_downloader(download_url, destination_path)
        relative_path = destination_path.relative_to(scope_directory).as_posix()
        artifact_records.append(
            ArtifactRecord(
                artifact_type=classify_artifact(artifact_file_name, artifact_source_relative_path),
                file_name=artifact_file_name or Path(artifact_relative_path).name,
                source_relative_path=artifact_relative_path,
                relative_path=relative_path,
                download_url=download_url,
            )
        )

    return RunRecord(
        job_name=job_name,
        job_run_number=run_number,
        run_url=normalized_run_url,
        requested_by_urls=sorted(set(resolved_run.requested_by_urls)),
        run_directory=run_directory.relative_to(scope_directory).as_posix(),
        artifact_count=len(artifact_records),
        artifacts=artifact_records,
    )


def run_downloader(
    config: DownloaderConfig,
    job_payload_fetcher: Callable[[str], dict[str, Any]] | None = None,
    build_payload_fetcher: Callable[[str], dict[str, Any]] | None = None,
    artifact_metadata_fetcher: Callable[[str], dict[str, Any]] | None = None,
    artifact_downloader: Callable[[str, Path], Path] | None = None,
) -> Path:
    validate_config(config)
    resolved_job_payload_fetcher = job_payload_fetcher or create_job_payload_fetcher(config)
    resolved_build_payload_fetcher = build_payload_fetcher or create_build_payload_fetcher(config)
    resolved_artifact_metadata_fetcher = artifact_metadata_fetcher or create_artifact_metadata_fetcher(config)
    resolved_artifact_downloader = artifact_downloader or create_artifact_downloader(config)

    source_mode, requested_urls, resolved_root_run_url, resolved_runs, failed_urls = _resolve_requested_runs(
        config=config,
        job_payload_fetcher=resolved_job_payload_fetcher,
        build_payload_fetcher=resolved_build_payload_fetcher,
    )
    scope_name = select_scope_name(config, source_mode=source_mode, resolved_root_run_url=resolved_root_run_url)
    output_root = Path(config.output.output_root).expanduser().resolve()
    scope_directory = output_root / scope_name
    scope_directory.mkdir(parents=True, exist_ok=True)

    run_records: list[RunRecord] = []
    for resolved_run in resolved_runs:
        try:
            run_records.append(
                _download_run_artifacts(
                    resolved_run=resolved_run,
                    scope_directory=scope_directory,
                    config=config,
                    artifact_metadata_fetcher=resolved_artifact_metadata_fetcher,
                    artifact_downloader=resolved_artifact_downloader,
                )
            )
        except Exception as error_details:
            failed_urls.append(FailedUrlRecord(url=resolved_run.run_url, error=str(error_details)))
            LOGGER.warning("Problem while downloading artifacts for %s: %s", resolved_run.run_url, error_details)
            LOGGER.warning("Traceback: %s", traceback.format_exc())

    if not run_records:
        raise RuntimeError("No Jenkins jobs were processed successfully")

    manifest = Manifest(
        created_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        source_mode=source_mode,
        scope_name=scope_name,
        requested_urls=requested_urls,
        resolved_root_run_url=resolved_root_run_url,
        build_selector=config.input.build_selector,
        failed_urls=failed_urls,
        runs=run_records,
    )
    manifest_path = manifest_path_for_scope(scope_directory)
    manifest.write_text(manifest_path)
    LOGGER.info("Manifest written: %s", manifest_path)
    return manifest_path


def parse_args() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Download Jenkins ZIP artifacts into a local scope directory")
    parser.add_argument("--config", required=False, help="Path to downloader JSON config", default="config.json")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    manifest_path = run_downloader(config)
    LOGGER.info("Download complete. Manifest: %s", manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
