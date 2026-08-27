"""Type stub for the firmware's frozen ``requests`` module (MicroPython's HTTP
client, not the PyPI package of the same name — see ``buddy_tts.py``'s
``_default_requests()``). Never shipped from this repository.

Declared narrowly for what ``buddy_tts.py`` actually calls: ``post()`` and the
handful of members it reads off the response.
"""

class _RawStream:
    def read(self, n: int) -> bytes | None: ...
    def settimeout(self, seconds: float) -> None: ...
    def close(self) -> None: ...

class Response:
    status_code: int
    raw: _RawStream

    def json(self) -> dict[str, object]: ...
    def close(self) -> None: ...

# `timeout` は micropython-lib の `requests` にはあるが、ファームウェアが
# どの版を焼いているかはこちらの持ち物ではない。取らない相手に当たったときは
# `TypeError` になり、`buddy/tts.py` の `_call_post` が timeout 無しへ落ちる。
def post(
    url: str,
    data: bytes | None = ...,
    headers: dict[str, str] | None = ...,
    timeout: float | None = ...,
) -> Response: ...
