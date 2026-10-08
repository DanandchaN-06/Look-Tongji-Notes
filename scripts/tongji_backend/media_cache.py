"""Serialize same-lecture CLI work across processes; locks release on exit."""
import os
import errno
import time
from contextlib import contextmanager
from pathlib import Path
from .progress import emit


@contextmanager
def lecture_lock(path: Path, timeout: float = 7200):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.resolve().is_relative_to(path.parent.resolve()):
        raise ValueError('课次锁包含外部文件链接')
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
                    raise TimeoutError('同一课次仍由另一个任务处理，请稍后继续')
                if not notified:
                    emit('transcript','waiting_cache')
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
