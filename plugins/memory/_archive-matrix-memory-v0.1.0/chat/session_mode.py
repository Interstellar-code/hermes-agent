def requires_session_mode(current_mode: str, required: str) -> None:
    if current_mode != required:
        raise RuntimeError(f"This tool requires session mode '{required}', got '{current_mode}'")
