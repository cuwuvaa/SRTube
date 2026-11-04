"""Minimal child launcher: apply kernel file-size limit, then exec without a shell."""

import os
import resource
import sys


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(2)
    limit = int(sys.argv[1])
    if limit <= 0:
        raise SystemExit(2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    # Fixed argv, never interpreted by a shell.
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)  # nosec B606


if __name__ == "__main__":
    main()
