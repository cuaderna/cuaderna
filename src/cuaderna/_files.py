from pathlib import Path


def read_text_within(path: str | Path, max_bytes: int) -> str:
    with Path(path).open("rb") as opened:
        content = opened.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError(f"the file is larger than the limit of {max_bytes} bytes")
    return content.decode("utf-8")
