"""A local DynamoDB and SQS for the first three acts, so nothing is created in your account.

`moto_server` is a real HTTP server speaking both. It is a separate process, which is the
point: act 2 kills the interpreter that parked the thread, and the parked thread has to
be somewhere that is not that interpreter's heap.

boto3 reads per-service endpoints from `AWS_ENDPOINT_URL_DYNAMODB` and
`AWS_ENDPOINT_URL_SQS`, so only those two services are redirected -- Bedrock still goes to
the real one, because the model is real. Nothing in `graph.py`, `handler.py` or the acts
mentions any of this. Point `AWS_ENDPOINT_URL_DYNAMODB` at DynamoDB Local
(`docker run -p 8000:8000 amazon/dynamodb-local`) and act 2 runs there instead; unset both
and the acts run against real AWS.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENDPOINT_VARS = ("AWS_ENDPOINT_URL_DYNAMODB", "AWS_ENDPOINT_URL_SQS")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LocalAws:
    """A `moto_server` subprocess, or nothing at all if the endpoints are already set."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.preset = {v: os.environ[v] for v in ENDPOINT_VARS if os.environ.get(v)}

    def __enter__(self) -> "LocalAws":
        if self.preset:  # somebody pointed us somewhere already
            return self
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "moto.server", "-p", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while True:
            if time.time() > deadline:
                raise RuntimeError("moto_server did not start")
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except urllib.error.HTTPError:
                break  # it answered, which is all we need
            except OSError:
                time.sleep(0.2)
        for var in ENDPOINT_VARS:
            os.environ[var] = url
        return self

    def __exit__(self, *exc: object) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=10)

    @property
    def child_env(self) -> dict[str, str]:
        """Environment for a subprocess that must talk to the same endpoints."""
        return dict(os.environ)
