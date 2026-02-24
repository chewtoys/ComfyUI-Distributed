def require_fields(data: dict, *fields) -> list[str]:
    """Return field names that are missing or empty in a JSON object."""
    if not isinstance(data, dict):
        return list(fields)

    missing = []
    for field in fields:
        if field not in data:
            missing.append(field)
            continue
        value = data.get(field)
        if value is None:
            missing.append(field)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(field)

    return missing


def validate_worker_id(worker_id: str, config: dict) -> bool:
    """Return True when worker_id exists in config['workers']."""
    worker_id_str = str(worker_id)
    workers = (config or {}).get("workers", [])
    return any(str(worker.get("id")) == worker_id_str for worker in workers)


def validate_positive_int(value, field_name: str) -> str | None:
    """Validate positive integers and return an error string when invalid."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return f"Field '{field_name}' must be a positive integer."
    if parsed <= 0:
        return f"Field '{field_name}' must be a positive integer."
    return None


def parse_positive_int(value, default: int) -> int:
    """Parse value as positive int, returning default on failure."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return max(1, int(default))
    return max(1, parsed)


def parse_positive_float(value, default: float) -> float:
    """Parse value as positive float, returning default on failure."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return max(0.0, float(default))
    return max(0.0, parsed)
