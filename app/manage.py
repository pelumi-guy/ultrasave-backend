import json
import os
import secrets
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from .config import Settings, token_hash


def write_atomic(path: Path, data: bytes):
    temporary = NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}-', delete=False)
    pending = Path(temporary.name)
    try:
        with temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def main():
    if len(sys.argv) != 2 or sys.argv[1] != 'init':
        raise SystemExit('Usage: python -m app.manage init')
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    hashes = settings.data_dir / 'token-hashes.json'
    token_path = settings.data_dir / 'device-token.txt'
    if hashes.exists():
        # Validate the whole configuration before reporting successful startup.
        settings.initialize()
        print('Already initialized. Existing device token and signing key were preserved.')
        return
    token = token_path.read_text(encoding='utf-8') if token_path.exists() else secrets.token_urlsafe(40)
    if not token:
        raise RuntimeError('The existing device token file is empty. Restore a valid token before initializing.')
    signing_path = settings.data_dir / 'signing-key'
    if not signing_path.exists():
        write_atomic(signing_path, secrets.token_bytes(32))
    if not token_path.exists():
        write_atomic(token_path, token.encode('utf-8'))
    # Publish the hashes last so a retry can reuse credentials after interruption.
    write_atomic(hashes, json.dumps([token_hash(token)]).encode('utf-8'))
    settings.initialize()
    print(f'Private backend initialized. Read the device token locally from {token_path}. Do not commit or share that file.')


if __name__ == '__main__':
    main()
