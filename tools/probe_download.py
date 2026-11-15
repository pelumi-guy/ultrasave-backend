"""Opt-in end-to-end provider check for an owned/authorized public video URL.

python -m tools.probe_download twitter https://x.com/user/status/123
Media stays in the ignored .data/probes directory; reports contain no source URL.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid
from app.config import Settings
from app.errors import ServiceError
from app.providers import ExtractionProvider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', choices=['twitter', 'threads', 'tiktok', 'youtube'])
    parser.add_argument('url')
    args = parser.parse_args()
    settings = Settings()
    destination = settings.data_dir / 'probes' / f'{args.source}-{uuid.uuid4().hex}'
    start = time.monotonic()
    report = {'source': args.source, 'url_hash': hashlib.sha256(args.url.encode()).hexdigest()}
    try:
        items = ExtractionProvider(args.source, settings).resolve(args.url, destination)
        report.update(success=True, items=items)
        for item in items:
            item['sha256'] = hashlib.sha256((destination / item['name']).read_bytes()).hexdigest()
    except ServiceError as error:
        report.update(success=False, error={'code': error.code, 'message': error.message})
    report['seconds'] = round(time.monotonic() - start, 2)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
