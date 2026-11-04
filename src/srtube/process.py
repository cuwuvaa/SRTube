import asyncio
import os
import signal
import sys
from pathlib import Path

from .errors import ProcessError, UserError


def child_environment() -> dict[str, str]:
    allowed = {"PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "HOME"}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    return result


async def run_process(
    *args: str,
    timeout: float,
    cwd: Path | None = None,
    max_output_bytes: int = 16_000_000,
    max_file_bytes: int = 220_000_000,
) -> bytes:
    """No shell; kill the entire process group on timeout or shutdown."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "srtube.limits",
        str(max_file_bytes),
        *args,
        cwd=cwd,
        env=child_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def read_stdout() -> bytes:
        output = bytearray()
        if process.stdout is None:
            raise RuntimeError("Missing subprocess stdout pipe")
        while chunk := await process.stdout.read(65536):
            output.extend(chunk)
            if len(output) > max_output_bytes:
                raise UserError("Ответ загрузчика превысил допустимый размер.")
        return bytes(output)

    async def read_stderr() -> bytes:
        output = bytearray()
        if process.stderr is None:
            raise RuntimeError("Missing subprocess stderr pipe")
        while chunk := await process.stderr.read(65536):
            output.extend(chunk)
            del output[:-65536]
        return bytes(output)

    readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await asyncio.gather(*readers)
            await process.wait()
    except BaseException as exc:
        # Grandchildren may still hold pipes after the group leader exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        # Drain paused pipe transports after killing the producer, or wait() can deadlock.
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, TimeoutError):
            raise UserError("Обработка заняла слишком много времени. Попробуй ещё раз позже.") from None
        raise
    if process.returncode:
        raise ProcessError(stderr.decode(errors="replace")[-8000:])
    return stdout
