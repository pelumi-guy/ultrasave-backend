"""Generate our own synthetic PNG and audible MP4. No social content is used."""
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


def generate():
    ROOT.mkdir(parents=True, exist_ok=True)
    def chunk(kind, content):
        return struct.pack('>I', len(content)) + kind + content + struct.pack('>I', zlib.crc32(kind + content))
    width, height = 320, 240
    pixels = b''.join(b'\x00' + b''.join(bytes((int(40 + x / width * 100), int(100 + y / height * 120), 150)) for x in range(width)) for y in range(height))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')
    (ROOT / 'ultrasave-sample.png').write_bytes(png)
    executable = shutil.which('ffmpeg')
    if not executable:
        try:
            import imageio_ffmpeg
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            raise SystemExit('Install requirements-dev.txt or FFmpeg to generate the sample video.')
    subprocess.run([executable, '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=12', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '2', '-c:v', 'libx264', '-profile:v', 'baseline', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(ROOT / 'ultrasave-sample.mp4')], check=True, timeout=60)
    print('Generated synthetic PNG and two-second H.264/AAC MP4 fixtures.')


if __name__ == '__main__':
    generate()
