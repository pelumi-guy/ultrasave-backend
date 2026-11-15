"""Local live-service check. Only synthetic fixtures and the local bootstrap token."""
import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from app.config import Settings


def main():
    token = (Settings().data_dir / 'device-token.txt').read_text().strip()
    base = 'http://127.0.0.1:8000'
    def api(path, body=None):
        request = Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    assert api('/v1/session')['authenticated']
    key = f'smoke-{uuid.uuid4()}'
    job = api('/v1/jobs', {'source': 'twitter', 'url': '', 'fixture': 'mixed', 'idempotency_key': key})
    assert api(f'/v1/requests/{key}')['id'] == job['id']
    deadline = time.monotonic() + 30
    while job['state'] in {'queued', 'resolving'} and time.monotonic() < deadline:
        time.sleep(.25)
        job = api(f'/v1/jobs/{job["id"]}')
    assert job['state'] == 'awaiting_selection', job['state']
    selected = [item['id'] for item in job['items']]
    job = api(f'/v1/jobs/{job["id"]}/select', {'item_ids': selected})
    for item in job['items']:
        parsed = urlsplit(item['url'])
        # Never send our bootstrap token to the artifact URL's host.
        with urlopen(base + parsed.path + '?' + parsed.query, timeout=10) as response:
            data = response.read()
        original = (Path(__file__).resolve().parents[1] / 'fixtures' / item['name']).read_bytes()
        assert hashlib.sha256(data).digest() == hashlib.sha256(original).digest()
        print(f'{item["kind"]}: {len(data)} bytes transferred and SHA-256 verified')
    assert api(f'/v1/jobs/{job["id"]}/ack', {'item_ids': selected})['state'] == 'completed'
    print('Live API + separate worker mixed-media lifecycle passed. No social media was fetched.')


if __name__ == '__main__':
    main()
