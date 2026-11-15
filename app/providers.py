"""Controlled fixtures and isolated, bounded public-video extraction."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Protocol
from .errors import ServiceError


class Provider(Protocol):
    def resolve(self, url: str, destination: Path) -> list[dict]: ...


class ExtractionProvider:
    def __init__(self, source, settings, should_continue=lambda: True):
        self.source, self.settings, self.should_continue = source, settings, should_continue

    def resolve(self, url: str, destination: Path) -> list[dict]:
        if self.source not in self.settings.live_sources:
            raise ServiceError('PROVIDER_NOT_READY', 'Live extraction for this source is disabled.', 501)
        destination.mkdir(parents=True, exist_ok=True)
        request = {'source': self.source, 'url': url, 'destination': str(destination.resolve()), 'limits': {
            'max_bytes': self.settings.max_media_bytes, 'max_items': self.settings.max_media_items,
            'max_duration': self.settings.max_duration_seconds,
        }}
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith('ULTRASAVE_') and not key.upper().endswith('_PROXY')}
        environment.update(YTDLP_NO_PLUGINS='1', PYTHONIOENCODING='utf-8')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen([sys.executable, '-m', 'app.extractor'],
                                       cwd=Path(__file__).resolve().parents[1], env=environment,
                                       stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL, **options)
            try:
                process.stdin.write(json.dumps(request).encode())
                process.stdin.close()
                deadline = time.monotonic() + self.settings.preparation_timeout
                while process.poll() is None:
                    if not self.should_continue():
                        raise ServiceError('CANCELLED', 'This preparation was cancelled or expired.')
                    if time.monotonic() >= deadline:
                        raise ServiceError('PREPARATION_TIMEOUT', 'Video preparation took too long. Try a shorter video.')
                    # Inputs plus a local muxed output can coexist temporarily.
                    size = sum(p.stat().st_size for p in destination.iterdir() if p.is_file())
                    if size > 2 * self.settings.max_media_bytes + 1024 * 1024:
                        raise ServiceError('MEDIA_TOO_LARGE', 'This collection exceeded its preparation budget.')
                    time.sleep(.25)
                output.seek(0)
                try:
                    response = json.loads(output.read(1024 * 1024))
                except (ValueError, UnicodeError):
                    raise ServiceError('EXTRACTION_FAILED', 'The extraction process could not return a result.')
                if response.get('error'):
                    error = response['error']
                    raise ServiceError(error['code'], error['message'])
                if process.returncode or not response.get('items'):
                    raise ServiceError('EXTRACTION_FAILED', 'The source returned no usable video.')
                return response['items']
            finally:
                if process.poll() is None:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)


class FixtureProvider:
    def resolve(self, url: str, destination: Path) -> list[dict]:
        import shutil
        fixtures = Path(__file__).resolve().parents[1] / 'fixtures'
        destination.mkdir(parents=True, exist_ok=True)
        items = []
        for item_id, filename, mime in [('image', 'ultrasave-sample.png', 'image/png'), ('video', 'ultrasave-sample.mp4', 'video/mp4')]:
            source = fixtures / filename
            if not source.is_file():
                raise ServiceError('FIXTURES_MISSING', 'Generate the sample files before starting the worker.', 503)
            shutil.copyfile(source, destination / filename)
            items.append({'id': item_id, 'name': filename, 'mime': mime, 'kind': mime.split('/')[0], 'bytes': source.stat().st_size})
        return items
