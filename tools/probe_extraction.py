"""Metadata-only probe using the same guarded adapters as the worker.

Install backend requirements, then run from backend:
python -m tools.probe_extraction twitter https://x.com/creator/status/123
Pass an actual owned/authorized sample URL, not the illustrative URL above.
"""
import argparse
import hashlib
import json
import time
from app.validation import validate_source_url
from app.extractor import metadata, public_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', choices=['twitter', 'threads', 'tiktok', 'youtube'])
    parser.add_argument('url')
    args = parser.parse_args()
    validate_source_url(args.source, args.url)
    try:
        import yt_dlp
        from yt_dlp.version import __version__
    except ImportError:
        raise SystemExit('Optional tool missing: install yt-dlp in your development environment to run this probe.')
    result = {'source': args.source, 'url_hash': hashlib.sha256(args.url.encode()).hexdigest(), 'extractor_version': __version__, 'timestamp': int(time.time()), 'download_tested': False}
    try:
        entries = metadata(args.source, args.url, 20)
        result.update(resolved=True, items=len(entries), format_counts=[len(entry.get('formats') or []) for entry in entries], live=any(entry.get('is_live') for entry in entries))
    except Exception as error:
        result.update(resolved=False, error=public_error(error))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
