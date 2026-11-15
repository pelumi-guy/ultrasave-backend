"""Input route validation; actual outbound connections are guarded by network.py."""
import ipaddress
import re
import socket
from urllib.parse import urlsplit
from .errors import ServiceError

HOSTS = {
    'twitter': {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'},
    'instagram': {'instagram.com', 'www.instagram.com'},
    'threads': {'threads.com', 'www.threads.com', 'threads.net', 'www.threads.net'},
    'tiktok': {'tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'},
    'youtube': {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'},
}


def validate_source_url(source: str, value: str):
    try:
        parsed = urlsplit(value)
        if source not in HOSTS or parsed.scheme != 'https' or parsed.hostname not in HOSTS[source] or parsed.username or parsed.password or parsed.port:
            raise ValueError()
        path = parsed.path
        routes = {
            'twitter': r'/(?:[^/]+/status|i/web/status)/\d+/?',
            'instagram': r'/(?:p|reel)/[\w-]+/?|/stories/[^/]+/\d+/?',
            'threads': r'/@[^/]+/post/[\w-]+/?|/share/[\w-]+/?',
            'tiktok': r'/@[^/]+/video/\d+/?|/t/[\w-]+/?',
        }
        if source == 'youtube':
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query)
            valid = 'list' not in query and (bool(re.fullmatch(r'/[\w-]{11}/?', path)) if parsed.hostname == 'youtu.be' else (path == '/watch' and bool(re.fullmatch(r'[\w-]{11}', query.get('v', [''])[0]))) or bool(re.fullmatch(r'/shorts/[\w-]{11}/?', path)))
        elif source == 'tiktok' and parsed.hostname in {'vm.tiktok.com', 'vt.tiktok.com'}:
            valid = bool(re.fullmatch(r'/[\w-]+/?', path))
        else:
            valid = bool(re.fullmatch(routes[source], path))
        if not valid:
            raise ValueError()
    except (ValueError, KeyError):
        raise ServiceError('INVALID_URL', 'Use a valid single post or video HTTPS link for the selected source.')
    return value


def public_addresses(host: str, lookup=socket.getaddrinfo):
    records = lookup(host, 443, type=socket.SOCK_STREAM)
    addresses = {record[4][0] for record in records}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ServiceError('UNSAFE_ADDRESS', 'The resolved address is not a public media host.')
    return addresses
