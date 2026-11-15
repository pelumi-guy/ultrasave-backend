"""Public Threads post parsing. Never select media from replies/recommendations."""
import html
import json
import re
from .errors import ServiceError


def post_videos(page: str, shortcode: str) -> list[dict]:
    posts = []
    # Threads embeds server-rendered post records in application/json scripts.
    for body in re.findall(r'<script\b[^>]*type=[\"\']application/(?:ld\+)?json[\"\'][^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = json.loads(html.unescape(body))
        except (ValueError, RecursionError):
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                if ((value.get('code') == shortcode or value.get('shortcode') == shortcode)
                        and any(key in value for key in ('media_type', 'video_versions', 'carousel_media', 'image_versions2'))):
                    posts.append(value)
                else:
                    stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(reversed(value))
    if not posts:
        raise ServiceError('CONTENT_UNAVAILABLE', 'Threads did not expose this public post. It may require login, be private, or have been removed.')
    post = next((p for p in posts if p.get('video_versions') or p.get('carousel_media')), posts[0])
    media = post.get('carousel_media') or [post]
    videos = []
    for item in media:
        versions = [v for v in item.get('video_versions', []) if isinstance(v, dict) and isinstance(v.get('url'), str)]
        if versions:
            videos.append({'id': str(item.get('pk') or len(videos) + 1),
                           'duration': item.get('video_duration'),
                           'formats': [{'url': v['url'], 'ext': 'mp4', 'protocol': 'https',
                                        'width': v.get('width'), 'height': v.get('height'),
                                        'vcodec': 'h264', 'acodec': 'unknown'} for v in versions]})
    if not videos:
        raise ServiceError('NO_MEDIA', 'This Threads post has no downloadable video.')
    return videos
