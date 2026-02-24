from .logging import debug_log, log


def trace_prefix(trace_execution_id: str) -> str:
    return f"[Distributed][exec:{trace_execution_id}]"


def trace_debug(trace_execution_id: str, message: str) -> None:
    debug_log(f"{trace_prefix(trace_execution_id)} {message}")


def trace_info(trace_execution_id: str, message: str) -> None:
    log(f"{trace_prefix(trace_execution_id)} {message}")
