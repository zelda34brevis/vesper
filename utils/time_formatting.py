from __future__ import annotations



def render_elapsed_time(total_elapsed_seconds_input) -> str:
	"""Render elapsed time as seconds (<60) or HH:MM:SS (>=60)."""
	total_elapsed_seconds = int(total_elapsed_seconds_input)
	if total_elapsed_seconds < 60:
		return f"{total_elapsed_seconds}s"

	hour_count, remaining_seconds = divmod(total_elapsed_seconds, 3600)
	minute_count, second_count = divmod(remaining_seconds, 60)
	return f"{hour_count:02d}:{minute_count:02d}:{second_count:02d}"
