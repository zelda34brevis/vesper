from __future__ import annotations

import json
import logging
import os
import traceback
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener as urllib_build_opener, urlopen

module_logger = logging.getLogger(__name__)

DOWNLOAD_CHUNK_SIZE_BYTES = 1024 * 1024
DEFAULT_FILE_DOWNLOAD_ATTEMPTS = 6
MAX_UNEXPECTED_FULL_RESPONSE_RESETS = 1
ResponseHeaders = Mapping[str, str]


def _open_url_response(
    target_url: str,
    url_opener,
    request_timeout_seconds: int,
    request_headers: Mapping[str, str] | None = None,
):
    """Open a URL and return the urllib response object."""
    prepared_request_headers: dict[str, str] = dict(request_headers or {})
    url_request = Request(target_url, headers=prepared_request_headers)
    if url_opener is not None:
        return url_opener.open(url_request, timeout=request_timeout_seconds)

    return urlopen(url_request, timeout=request_timeout_seconds)


def _get_response_status_code(http_response) -> int | None:
    """Read the HTTP status code from a urllib response object."""
    if hasattr(http_response, "status"):
        return http_response.status

    if hasattr(http_response, "getcode"):
        return http_response.getcode()

    return None


def _extract_expected_total_bytes_from_headers(
    response_headers: ResponseHeaders,
    range_start_byte: int,
) -> int:
    """Resolve the expected full file size using Content-Range or Content-Length headers."""
    content_range_header = str(response_headers.get("Content-Range") or "").strip()
    if content_range_header:
        _, _, total_size_text = content_range_header.partition("/")
        if total_size_text.isdigit():
            return int(total_size_text)

    content_length_header = str(response_headers.get("Content-Length") or "").strip()
    if content_length_header.isdigit():
        content_length_value = int(content_length_header)
        if range_start_byte:
            return range_start_byte + content_length_value
        return content_length_value

    raise RuntimeError("Unable to determine the remote file size from response headers")


def _extract_expected_total_bytes(http_response, range_start_byte: int) -> int:
    """Resolve the expected full file size using Content-Range or Content-Length."""
    return _extract_expected_total_bytes_from_headers(http_response.headers, range_start_byte=range_start_byte)


def _stream_response_into_file(
    http_response,
    destination_path: Path,
    chunk_size_bytes: int = DOWNLOAD_CHUNK_SIZE_BYTES,
) -> int:
    """Append the current HTTP response body into destination_path."""
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
    """Return request headers for a resumed download attempt."""
    if resume_from_byte <= 0:
        return {}

    return {"Range": f"bytes={resume_from_byte}-"}


def _recover_completed_partial_file_from_http_416(
    http_error,
    partial_output_path: Path,
    output_file_path: Path,
    source_url: str,
) -> bool:
    """Finalize a fully downloaded .part file when the server rejects a resume request with HTTP 416."""
    if http_error.code != 416 or not partial_output_path.exists():
        return False

    try:
        expected_total_bytes = _extract_expected_total_bytes_from_headers(
            http_error.headers or {},
            range_start_byte=0,
        )
        downloaded_file_size = partial_output_path.stat().st_size
        if downloaded_file_size != expected_total_bytes:
            return False

        os.replace(partial_output_path, output_file_path)
        module_logger.info(
            f"Recovered completed partial download after HTTP 416: {source_url} -> {output_file_path}"
        )
        return True
    except Exception as error_details:
        module_logger.warning(
            f"Error: Could not recover completed partial download after HTTP 416 for {source_url}: {error_details}"
        )
        module_logger.warning(f"Stack trace: {traceback.format_exc()}")
        return False


def _should_restart_from_scratch_after_unexpected_resume_response(
    response_status_code: int | None,
    resumed_from_byte: int,
    unexpected_full_response_reset_count: int,
    partial_output_path: Path,
    source_url: str,
) -> bool:
    """Handle servers that ignore Range and return a full response during resume."""
    if resumed_from_byte <= 0 or response_status_code == 206:
        return False

    if unexpected_full_response_reset_count >= MAX_UNEXPECTED_FULL_RESPONSE_RESETS:
        raise RuntimeError(
            f"Resume request for {source_url} returned HTTP {response_status_code} after a prior reset; keeping {partial_output_path}"
        )

    module_logger.warning(
        f"Resume request for {source_url} returned HTTP {response_status_code} instead of 206; deleting {partial_output_path} and retrying from scratch"
    )
    partial_output_path.unlink(missing_ok=True)
    return True


def _finalize_download_if_complete(
    partial_output_path: Path,
    output_file_path: Path,
    expected_total_bytes: int,
    source_url: str,
) -> bool:
    """Promote a fully downloaded .part file to the final destination path."""
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


def _open_download_response(
    source_url: str,
    url_opener,
    request_timeout_seconds: int,
    request_headers: Mapping[str, str],
):
    """Open one binary download attempt."""
    return _open_url_response(
        target_url=source_url,
        url_opener=url_opener,
        request_timeout_seconds=request_timeout_seconds,
        request_headers=request_headers,
    )


def fetch_url_bytes(target_url, url_opener, request_timeout_seconds) -> bytes:
    """Fetch raw bytes from a URL using an optional opener."""
    http_response = _open_url_response(
        target_url=target_url,
        url_opener=url_opener,
        request_timeout_seconds=request_timeout_seconds,
    )

    with http_response:
        return http_response.read()


def download_url_to_file(
    source_url: str,
    output_file_path,
    url_opener,
    request_timeout_seconds: int,
    max_file_download_attempts: int = DEFAULT_FILE_DOWNLOAD_ATTEMPTS,
) -> Path:
    """Download URL content into output_file_path with resume support for binary files."""
    output_file_path = Path(output_file_path)
    output_file_path.parent.mkdir(parents=True, exist_ok=True)
    if output_file_path.exists():
        return output_file_path

    partial_output_path = output_file_path.parent / f"{output_file_path.name}.part"
    last_error = None
    unexpected_full_response_reset_count = 0

    for attempt_number in range(1, max_file_download_attempts + 1):
        resumed_from_byte = partial_output_path.stat().st_size if partial_output_path.exists() else 0
        request_headers = _make_range_request_headers(resume_from_byte=resumed_from_byte)

        try:
            http_response = _open_download_response(
                source_url=source_url,
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

            module_logger.warning(
                f"Error: Could not open {source_url} for download on attempt {attempt_number}/{max_file_download_attempts}: {error_details}"
            )
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
            raise
        except Exception as error_details:
            last_error = error_details
            module_logger.warning(
                f"Error: Download open failed for {source_url} on attempt {attempt_number}/{max_file_download_attempts}: {error_details}"
            )
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
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
                module_logger.warning(f"Error: {last_error}")
                break

            if should_restart_from_scratch:
                unexpected_full_response_reset_count += 1
                continue

            try:
                expected_total_bytes = _extract_expected_total_bytes(http_response, range_start_byte=resumed_from_byte)
            except Exception as error_details:
                last_error = error_details
                module_logger.warning(
                    f"Error: Could not resolve file size for {source_url} on attempt {attempt_number}/{max_file_download_attempts}: {error_details}"
                )
                module_logger.warning(f"Stack trace: {traceback.format_exc()}")
                break

            try:
                _stream_response_into_file(http_response=http_response, destination_path=partial_output_path)
            except IncompleteRead as error_details:
                last_error = error_details
                module_logger.warning(
                    f"Error: Incomplete download for {source_url} on attempt {attempt_number}/{max_file_download_attempts}: {error_details}"
                )
                module_logger.warning(f"Stack trace: {traceback.format_exc()}")
                if attempt_number == max_file_download_attempts:
                    break
                continue
            except Exception as error_details:
                last_error = error_details
                module_logger.warning(
                    f"Error: Download stream failed for {source_url} on attempt {attempt_number}/{max_file_download_attempts}: {error_details}"
                )
                module_logger.warning(f"Stack trace: {traceback.format_exc()}")
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
                return output_file_path
        except RuntimeError as error_details:
            last_error = error_details
            module_logger.warning(f"Error: {last_error}")

        if attempt_number == max_file_download_attempts:
            break

    if last_error is None:
        last_error = RuntimeError(f"Unable to download the file from {source_url}")

    raise RuntimeError(f"Unable to download the file from {source_url}") from last_error


def fetch_url_json(target_url, url_opener, request_timeout_seconds) -> dict[str, Any]:
    """Fetch a JSON object from a URL using an optional opener."""
    payload_bytes = fetch_url_bytes(target_url=target_url, url_opener=url_opener, request_timeout_seconds=request_timeout_seconds)
    return json.loads(payload_bytes.decode("utf-8"))


def create_url_opener(jenkins_username, jenkins_api_token):
    """Create a urllib opener with optional basic auth; return None if credentials are absent."""
    if not jenkins_username or not jenkins_api_token:
        return None

    password_manager = HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, uri="*", user=jenkins_username, passwd=jenkins_api_token)
    return urllib_build_opener(HTTPBasicAuthHandler(password_manager))


def compose_job_path(display_job_name) -> str:
    """Convert a Jenkins job name into a /job/... path that supports nested folders."""
    prepared_job_name = str(display_job_name).replace("»", "/")
    path_segments = [name_segment.strip() for name_segment in prepared_job_name.split("/") if name_segment.strip()]
    encoded_segments = [quote(name_segment, safe="") for name_segment in path_segments]
    return "/".join(f"job/{name_segment}" for name_segment in encoded_segments)


def parse_job_name_and_run_number(run_base_url) -> tuple[str, str]:
    """Read the Jenkins job path and run number from a Jenkins run URL.

    For nested jobs (``/job/folder/job/child/<run>/``), it returns ``folder/child``
    so cache keys and report filenames remain collision-resistant.
    """
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


def normalize_run_url(raw_url) -> str:
    """Return a canonical Jenkins run URL without query or fragment and with a trailing slash."""
    sanitized_value = str(raw_url).strip()
    url_parts = urlsplit(sanitized_value)
    normalized_path = url_parts.path if url_parts.path.endswith("/") else f"{url_parts.path}/"
    return urlunsplit((url_parts.scheme, url_parts.netloc, normalized_path, "", ""))


