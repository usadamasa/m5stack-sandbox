"""Type stub for MicroPython's ``select`` — the slice this repository uses.

CPython's typeshed ``select.poll`` registers **file descriptors** and hands
back ``(fd, event)``. MicroPython registers the stream or socket **object**
itself and hands that back, which is what ``buddy/serial.py`` and
``buddy/netlink.py`` rely on: a socket has no ``fileno()`` on this port.
"""

POLLIN: int
POLLOUT: int
POLLERR: int
POLLHUP: int

class Poller:
    def register(self, obj: object, eventmask: int = ..., /) -> None: ...
    def unregister(self, obj: object, /) -> None: ...
    def poll(self, timeout: int = ..., /) -> list[tuple[object, int]]: ...
    def ipoll(self, timeout: int = ..., flags: int = ..., /) -> object: ...

def poll() -> Poller: ...
