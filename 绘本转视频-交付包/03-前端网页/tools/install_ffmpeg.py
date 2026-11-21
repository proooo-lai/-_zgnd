"""Install Windows FFmpeg and ffprobe from a single upstream release archive."""
from pathlib import Path
from urllib.request import urlretrieve
from tempfile import TemporaryDirectory
from zipfile import ZipFile
import os
import shutil

root = Path(__file__).resolve().parents[1] / "tools" / "bin"
if __name__ == "__main__":
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        if not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")):
            raise SystemExit("请使用系统包管理器安装 ffmpeg 与 ffprobe。")
    elif not all((root / name).is_file() for name in ("ffmpeg.exe", "ffprobe.exe")):
        with TemporaryDirectory() as temporary:
            archive = Path(temporary) / "ffmpeg.zip"
            urlretrieve("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", archive)
            with ZipFile(archive) as zipped:
                for name in zipped.namelist():
                    if name.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                        (root / Path(name).name).write_bytes(zipped.read(name))
    print("FFmpeg and ffprobe ready")
