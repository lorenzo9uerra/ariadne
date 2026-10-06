"""Write staged files into a container directory, refusing links and FIFOs.

Arguments: target directory. Standard input: JSON of name to base64 data.
"""

import base64
import json
import os
import stat
import sys

directory = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for name, encoded in json.load(sys.stdin).items():
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Upload destination must be a regular file")
            os.ftruncate(fd, 0)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(base64.b64decode(encoded, validate=True))
        finally:
            os.close(fd)
finally:
    os.close(directory)
