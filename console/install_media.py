"""User-invoked installation of the separately distributed FFmpeg tool."""
import shutil
import subprocess
import sys

winget=shutil.which('winget')
if not winget:
    print('未找到 Windows 程序安装器。请从 https://www.gyan.dev/ffmpeg/builds/ 安装 FFmpeg，将 ffmpeg.exe 放进 app/bin，或加入 PATH。')
    raise SystemExit(1)
print('将通过 Windows 程序安装器安装 Gyan.FFmpeg。请阅读安装器显示的许可。')
result=subprocess.run([winget,'install','--id','Gyan.FFmpeg','--exact','--source','winget'])
if result.returncode:
    print('安装未完成。你仍可以使用管理界面和已有阅读网页；字幕转写需要有效 FFmpeg。')
else:
    print('安装完成。重新打开管理控制台，检查环境状态。')
raise SystemExit(result.returncode)
