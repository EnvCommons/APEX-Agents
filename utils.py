import tempfile
from pathlib import Path

from openreward import SandboxesAPI


async def upload_text(
    sandbox: SandboxesAPI,
    remote_path: str,
    content: str,
    *,
    ensure_trailing_newline: bool = False,
) -> None:
    """Upload text content to a file on the remote computer."""
    data = content if not ensure_trailing_newline or content.endswith("\n") else f"{content}\n"

    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.write(data)
        tmp.flush()
        temp_path = Path(tmp.name)

    try:
        await sandbox.upload(str(temp_path), remote_path)
    finally:
        temp_path.unlink(missing_ok=True)


async def download_text(
    sandbox: SandboxesAPI,
    remote_path: str,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> str:
    """Download text content from the remote computer."""
    file_bytes = await sandbox.download(remote_path)
    return file_bytes.decode(encoding, errors)
