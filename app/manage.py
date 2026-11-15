import json
import secrets
import sys
from .config import Settings, token_hash


def main():
    if len(sys.argv) != 2 or sys.argv[1] != 'init':
        raise SystemExit('Usage: python -m app.manage init')
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    hashes = settings.data_dir / 'token-hashes.json'
    token_path = settings.data_dir / 'device-token.txt'
    if hashes.exists():
        print('Already initialized. Existing device token and signing key were preserved.')
        return
    token = secrets.token_urlsafe(40)
    token_path.write_text(token, encoding='utf-8')
    hashes.write_text(json.dumps([token_hash(token)]), encoding='utf-8')
    (settings.data_dir / 'signing-key').write_bytes(secrets.token_bytes(32))
    print(f'Private backend initialized. Read the device token locally from {token_path}. Do not commit or share that file.')


if __name__ == '__main__':
    main()
