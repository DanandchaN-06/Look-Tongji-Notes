"""Serialize same-lecture CLI work across processes; locks release on exit."""
import os
import errno
import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from .progress import emit


@contextmanager
def lecture_lock(path: Path, timeout: float = 7200):
    with file_lock(path, timeout=timeout, on_wait=lambda: emit('transcript','waiting_cache')):
        yield


@contextmanager
def file_lock(path: Path, timeout: float = 7200, on_wait=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.resolve().is_relative_to(path.parent.resolve()):
        raise ValueError('锁文件包含外部文件链接')
    with path.open('a+b') as handle:
        if handle.seek(0,2)==0:
            handle.write(b'0')
            handle.flush()
        start=time.monotonic()
        notified=False
        while True:
            handle.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES,errno.EAGAIN):
                    raise
                if time.monotonic()-start>=timeout:
                    raise TimeoutError('资源仍由另一个任务处理，请稍后继续')
                if not notified:
                    if on_wait:
                        on_wait()
                    notified=True
                time.sleep(.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


@contextmanager
def account_auth_refresh_lock(state_dir: Path, username: str, timeout: float = 7200):
    identity = hashlib.sha256(username.strip().casefold().encode("utf-8")).hexdigest()
    with file_lock(Path(state_dir) / "auth-refresh" / f"{identity}.lock", timeout=timeout):
        yield
