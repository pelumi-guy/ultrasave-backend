import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('ULTRASAVE_DATA_DIR', str(ROOT / '.data'))))
    database_url: str = field(default_factory=lambda: os.getenv('ULTRASAVE_DATABASE_URL', ''))
    public_url: str = field(default_factory=lambda: os.getenv('ULTRASAVE_PUBLIC_URL', 'http://10.0.2.2:8000'))
    fixtures_enabled: bool = field(default_factory=lambda: os.getenv('ULTRASAVE_FIXTURES', 'true').lower() == 'true')
    token_hashes: set[str] = field(default_factory=set)
    signing_key: bytes = b''
    retention_seconds: int = 86400
    daily_jobs: int = 30
    active_jobs: int = 4
    live_sources: frozenset[str] = field(default_factory=lambda: frozenset(
        value.strip() for value in os.getenv('ULTRASAVE_LIVE_SOURCES', 'twitter,threads,tiktok,youtube').split(',') if value.strip()))
    max_media_bytes: int = field(default_factory=lambda: int(os.getenv('ULTRASAVE_MAX_MEDIA_BYTES', '157286400')))
    max_duration_seconds: int = field(default_factory=lambda: int(os.getenv('ULTRASAVE_MAX_DURATION_SECONDS', '900')))
    max_media_items: int = 20
    preparation_timeout: int = field(default_factory=lambda: int(os.getenv('ULTRASAVE_PREPARATION_TIMEOUT', '240')))

    def initialize(self):
        if not self.live_sources <= {'twitter', 'threads', 'tiktok', 'youtube'}:
            raise RuntimeError('ULTRASAVE_LIVE_SOURCES contains an unknown or unimplemented source.')
        if not (1 <= self.max_media_items <= 30 and 1 <= self.max_media_bytes <= 1024 ** 3
                and 1 <= self.max_duration_seconds <= 3600 and 1 <= self.preparation_timeout <= 600):
            raise RuntimeError('Invalid extraction resource limits.')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f'sqlite:///{(self.data_dir / "jobs.db").as_posix()}'
        token_path = self.data_dir / 'token-hashes.json'
        if not self.token_hashes and token_path.exists():
            self.token_hashes = set(json.loads(token_path.read_text()))
        if not self.token_hashes:
            raise RuntimeError('No device tokens configured. Run python -m app.manage init first.')
        if not self.signing_key:
            path = self.data_dir / 'signing-key'
            if not path.exists():
                raise RuntimeError('Signing key missing. Run python -m app.manage init first.')
            self.signing_key = path.read_bytes()
        return self


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
