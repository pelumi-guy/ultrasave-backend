"""Bounded extraction child process. Only normalized local MP4s leave this process."""
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request

from .errors import ServiceError
from .network import DOMAINS, checked_url, public_opener, read_limited
from .threads import post_videos
from .validation import validate_source_url


class QuietLogger:
    def debug(self, _message): pass
    def warning(self, _message): pass
    def error(self, _message): pass


def ffmpeg_path():
    executable = shutil.which('ffmpeg')
    if not executable:
        try:
            import imageio_ffmpeg
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            pass
    if not executable:
        raise ServiceError('DEPENDENCY_MISSING', 'The backend needs FFmpeg installed to prepare videos.', 503)
    return executable


def metadata(source, url, max_items):
    if source == 'threads':
        with public_opener(source).open(url, timeout=15) as response:
            final = response.geturl()
            page = read_limited(response, 8 * 1024 * 1024).decode('utf-8', errors='replace')
        match = re.fullmatch(r'/@[^/]+/post/([\w-]+)/?', urlsplit(final).path)
        if not match:
            # Some share pages use a canonical link instead of an HTTP redirect.
            candidates = re.findall(r'<link\b[^>]*rel=[\"\']canonical[\"\'][^>]*href=[\"\']([^\"\']+)', page, re.I)
            for candidate in candidates:
                checked_url(candidate, DOMAINS[source])
                match = re.fullmatch(r'/@[^/]+/post/([\w-]+)/?', urlsplit(candidate).path)
                if match:
                    break
        if not match:
            raise ServiceError('CONTENT_UNAVAILABLE', 'Threads could not open this share link. Try the original public post link.')
        try:
            return post_videos(page, match[1])
        except ServiceError as error:
            if error.code != 'CONTENT_UNAVAILABLE':
                raise
            # Public link-preview pages can carry media omitted from the app shell.
            # No account session, login cookies, or private-post access is used.
            preview = Request(final, headers={'User-Agent': 'Googlebot', 'Accept-Encoding': 'identity'})
            with public_opener(source).open(preview, timeout=15) as response:
                page = read_limited(response, 8 * 1024 * 1024).decode('utf-8', errors='replace')
            return post_videos(page, match[1])

    from yt_dlp import YoutubeDL
    from yt_dlp.networking._urllib import UrllibRH
    from yt_dlp.networking.common import Response

    if source == 'youtube' and not shutil.which('node'):
        raise ServiceError('DEPENDENCY_MISSING', 'The backend needs Node.js installed to resolve YouTube videos.', 503)

    class BoundedResponse(Response):
        def __init__(self, response):
            super().__init__(response, response.url, response.headers, response.status)
            self.remaining = 8 * 1024 * 1024

        def read(self, amt=None):
            data = super().read(min(amt, self.remaining + 1) if amt is not None and amt >= 0 else self.remaining + 1)
            self.remaining -= len(data)
            if self.remaining < 0:
                self.close()
                raise ServiceError('RESPONSE_TOO_LARGE', 'The source response is too large.')
            return data

    class PublicUrllibRH(UrllibRH):
        _SUPPORTED_URL_SCHEMES = ('https',)

        def _get_proxies(self, _request):
            return {}

        def _prepare_headers(self, _request, headers):
            headers['Accept-Encoding'] = 'identity'

        def _create_instance(self, proxies, cookiejar, legacy_ssl_support=None):
            return public_opener(source, cookiejar)

        def _send(self, request):
            checked_url(request.url, DOMAINS[source])
            return BoundedResponse(super()._send(request))

    class PublicYoutubeDL(YoutubeDL):
        def build_request_director(self, _handlers, preferences=None):
            # No requests/curl fallback, system proxies, file URLs, or generic fetcher.
            return super().build_request_director([PublicUrllibRH])

    parsed = urlsplit(url)
    if source == 'twitter':
        url = urlunsplit(('https', 'x.com', parsed.path, '', ''))
    elif source == 'tiktok':
        if parsed.hostname in {'vt.tiktok.com', 'vm.tiktok.com'} or parsed.path.startswith('/t/'):
            with public_opener(source).open(Request(url, method='HEAD'), timeout=15) as response:
                url = response.geturl()
            validate_source_url(source, url)
            parsed = urlsplit(url)
        url = urlunsplit(('https', 'www.tiktok.com', parsed.path, '', ''))
    options = {
        'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
        'noprogress': True, 'skip_download': True, 'noplaylist': True,
        'playlistend': max_items + 1, 'ignoreerrors': False,
        'socket_timeout': 15, 'retries': 1, 'extractor_retries': 1,
        'cachedir': False, 'proxy': '', 'check_formats': False,
        'js_runtimes': {'node': {}}, 'remote_components': [],
        'format': 'bestvideo+bestaudio/best',
    }
    key = {'twitter': 'Twitter', 'tiktok': 'TikTok', 'youtube': 'Youtube'}[source]
    with PublicYoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False, ie_key=key)
        if info:
            # Keep only anonymous, URL-scoped playback cookies in this child.
            # They never enter job metadata, logs, or the mobile app.
            for entry in info.get('entries') or [info]:
                if not entry:
                    continue
                for media in entry.get('formats', []):
                    cookie = downloader.cookiejar.get_cookie_header(media.get('url', ''))
                    if cookie:
                        media['http_headers'] = {**media.get('http_headers', {}), 'Cookie': cookie}
    if not info:
        raise ServiceError('NO_MEDIA', 'This post has no downloadable video.')
    if source == 'youtube' and info.get('_type') in {'playlist', 'multi_video'}:
        raise ServiceError('UNSUPPORTED_CONTENT', 'Only a single YouTube video is supported.')
    entries = list(info.get('entries') or [info])
    if not entries or any(not isinstance(entry, dict) for entry in entries):
        raise ServiceError('CONTENT_UNAVAILABLE', 'The source did not return a complete media collection.')
    return entries


def number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else 0


def select_formats(info, source):
    """Prefer H.264 MP4; mux YouTube's separate AAC stream when needed."""
    formats = [f for f in info.get('formats', []) if f.get('url')
               and urlsplit(f['url']).scheme == 'https'
               and f.get('protocol', 'https') == 'https' and not f.get('has_drm')]
    video = [f for f in formats if f.get('ext') == 'mp4' and f.get('vcodec') != 'none'
             and number(f.get('height')) <= 1080
             and (not f.get('vcodec') or f['vcodec'].startswith(('avc', 'h264', 'unknown')))]
    rank = lambda f: (number(f.get('height')), number(f.get('tbr')), number(f.get('width')))
    combined = [f for f in video if f.get('acodec') != 'none']
    best_combined = max(combined, key=rank, default=None)
    if source == 'youtube':
        separate = [f for f in video if f.get('acodec') == 'none']
        audio = [f for f in formats if f.get('vcodec') == 'none' and f.get('ext') in {'m4a', 'mp4'}
                 and str(f.get('acodec', '')).startswith(('mp4a', 'aac'))]
        best_video = max(separate, key=rank, default=None)
        if best_video and audio and (not best_combined or rank(best_video) > rank(best_combined)):
            return [best_video, max(audio, key=lambda f: number(f.get('abr')))]
    if best_combined:
        return [best_combined]
    raise ServiceError('UNSUPPORTED_FORMAT', 'No compatible downloadable MP4 was available. Live, protected, or manifest-only streams are not supported.')


def download_file(opener, media, path, budget, max_bytes):
    expected = number(media.get('filesize'))
    if expected > max_bytes - budget[0]:
        raise ServiceError('MEDIA_TOO_LARGE', 'This collection exceeds the backend file-size limit.')
    # Forward only playback headers. Never forward service tokens or arbitrary Host headers.
    allowed = {'user-agent', 'referer', 'origin', 'accept', 'accept-language', 'sec-fetch-mode', 'cookie'}
    headers = {k: v for k, v in media.get('http_headers', {}).items() if k.lower() in allowed}
    headers['Accept-Encoding'] = 'identity'
    with opener.open(Request(media['url'], headers=headers), timeout=15) as response, path.open('wb') as output:
        length = response.headers.get('Content-Length')
        if length and int(length) > max_bytes - budget[0]:
            raise ServiceError('MEDIA_TOO_LARGE', 'This collection exceeds the backend file-size limit.')
        written = 0
        while chunk := response.read(256 * 1024):
            written += len(chunk)
            budget[0] += len(chunk)
            if budget[0] > max_bytes:
                raise ServiceError('MEDIA_TOO_LARGE', 'This collection exceeds the backend file-size limit.')
            output.write(chunk)
        if not written or (length and written != int(length)):
            raise ServiceError('INCOMPLETE_MEDIA', 'The source transfer was incomplete. Try the link again.')


def prepare(source, url, destination, limits):
    validate_source_url(source, url)
    executable = ffmpeg_path()
    entries = metadata(source, url, limits['max_items'])
    if len(entries) > limits['max_items']:
        raise ServiceError('TOO_MANY_ITEMS', 'This post exceeds the backend collection limit.')
    destination.mkdir(parents=True, exist_ok=True)
    opener = public_opener(source)
    budget, items = [0], []
    for index, info in enumerate(entries, 1):
        if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'} or info.get('has_drm'):
            raise ServiceError('UNSUPPORTED_CONTENT', 'Live, upcoming, and protected videos are not supported.')
        if number(info.get('duration')) > limits['max_duration']:
            raise ServiceError('MEDIA_TOO_LONG', 'This video exceeds the backend duration limit.')
        formats = select_formats(info, source)
        inputs = []
        for part, media in enumerate(formats):
            # Metadata-level playback headers apply when a format has no override.
            media = {**media, 'http_headers': {**info.get('http_headers', {}), **media.get('http_headers', {})}}
            target = destination / f'input-{index}-{part}.mp4'
            download_file(opener, media, target, budget, limits['max_bytes'])
            inputs.append(target)
        name = f'{source}-{index:02d}.mp4'
        output = destination / name
        command = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y']
        for target in inputs:
            # Force the MP4/M4A demuxer: a hostile response cannot masquerade as
            # a concat/HLS file referencing other local files or network URLs.
            command += ['-protocol_whitelist', 'file,pipe', '-f', 'mov',
                        '-enable_drefs', '0', '-use_absolute_path', '0', '-i', str(target)]
        command += ['-map', '0:v:0', '-map', '1:a:0' if len(inputs) == 2 else '0:a:0?',
                    '-c', 'copy', '-movflags', '+faststart', str(output)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        if result.returncode or not output.is_file() or output.stat().st_size == 0:
            raise ServiceError('PROCESSING_FAILED', 'The source file could not be prepared as an MP4.')
        # Inspect local streams, even when the provider omits duration or claims audio.
        probe = subprocess.run([executable, '-hide_banner', '-nostdin', '-protocol_whitelist', 'file,pipe',
                                '-i', str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
        details = probe.stderr.decode('utf-8', errors='replace')
        duration = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', details)
        if not duration or 'Video:' not in details:
            raise ServiceError('INVALID_MEDIA', 'The prepared file has no valid video stream or duration.')
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        if seconds > limits['max_duration']:
            raise ServiceError('MEDIA_TOO_LONG', 'This video exceeds the backend duration limit.')
        if len(inputs) == 2 and 'Audio:' not in details:
            raise ServiceError('INVALID_MEDIA', 'The merged video is missing its audio stream.')
        for target in inputs:
            target.unlink()
        items.append({'id': f'video-{index}', 'name': name, 'mime': 'video/mp4', 'kind': 'video',
                      'bytes': output.stat().st_size, 'duration': seconds, 'has_audio': 'Audio:' in details})
    if sum(item['bytes'] for item in items) > limits['max_bytes']:
        raise ServiceError('MEDIA_TOO_LARGE', 'The prepared collection exceeds the backend size limit.')
    return items


def public_error(error):
    if isinstance(error, ServiceError):
        return {'code': error.code, 'message': error.message}
    text = str(error).lower()  # Classify only; never expose extractor messages/URLs/cookies.
    if any(word in text for word in ('429', 'rate limit', 'too many requests')):
        return {'code': 'SOURCE_RATE_LIMITED', 'message': 'The source is rate-limiting this server. Please try later.'}
    if any(word in text for word in ('sign in', 'login', 'log in', 'confirm you', 'bot', 'captcha', '403')):
        return {'code': 'SOURCE_ACCESS_REQUIRED', 'message': 'The source requires login or blocked this server. Try a public link again later.'}
    if any(word in text for word in ('private', 'removed', 'unavailable', 'not found', '404', 'no video')):
        return {'code': 'CONTENT_UNAVAILABLE', 'message': 'This content is private, removed, unavailable, or has no downloadable video.'}
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return {'code': 'PREPARATION_TIMEOUT', 'message': 'Video preparation took too long. Try a shorter video.'}
    return {'code': 'EXTRACTION_FAILED', 'message': 'The source could not provide a compatible video. Try the original public post link again later.'}


def main():
    try:
        request = json.loads(sys.stdin.read(8192))
        items = prepare(request['source'], request['url'], Path(request['destination']), request['limits'])
        response = {'items': items}
    except Exception as error:
        response = {'error': public_error(error)}
    print(json.dumps(response))


if __name__ == '__main__':
    main()
