"""Find a runnable FFmpeg, not merely a filename present in PATH."""
import os
import shutil
import subprocess
from pathlib import Path


class MediaToolError(RuntimeError):
    pass


_validated = {}


def candidates() -> list[Path]:
    override = os.environ.get('LOOK_TONGJI_FFMPEG','').strip()
    if override:
        return [Path(override).expanduser()]
    result=[]
    found=shutil.which('ffmpeg')
    if found:
        result.append(Path(found))
    local=os.environ.get('LOCALAPPDATA')
    if os.name=='nt' and local:
        packages=Path(local)/'Microsoft'/'WinGet'/'Packages'
        if packages.is_dir():
            result.extend(sorted(packages.glob('*FFmpeg*/**/bin/ffmpeg.exe'),reverse=True))
    return list(dict.fromkeys(result))


def resolve_ffmpeg() -> str:
    errors=[]
    for path in candidates():
        try:
            info=path.stat()
            if info.st_size==0:
                raise ValueError('文件为空，命令入口已损坏')
            key=(str(path.resolve()),info.st_size,info.st_mtime_ns)
            if key in _validated:
                return str(path)
            result=subprocess.run([str(path),'-version'],stdin=subprocess.DEVNULL,capture_output=True,
                text=True,encoding='utf-8',errors='replace',timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            if result.returncode!=0 or not result.stdout.lower().startswith('ffmpeg version'):
                raise ValueError('版本检查失败，文件不是可用的 FFmpeg')
            _validated[key]=True
            return str(path)
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            errors.append(f'{path}: {exc}')
    detail=errors[0] if errors else '没有找到 ffmpeg 可执行文件'
    raise MediaToolError(f'FFmpeg 无法运行：{detail}。请安装适合当前系统的版本，或设置 LOOK_TONGJI_FFMPEG 为可执行文件的完整路径。')
