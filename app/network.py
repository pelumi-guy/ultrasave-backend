"""HTTPS-only egress shared by metadata extraction and media streaming.

Resolve once, reject every non-public answer, and connect to the checked numeric
address while retaining the original hostname for certificate verification/SNI.
Redirects go through the same policy; no system proxy or file handler is loaded.
"""
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit
from urllib.request import (
    HTTPSHandler, HTTPRedirectHandler, HTTPErrorProcessor, HTTPDefaultErrorHandler,
    HTTPCookieProcessor, OpenerDirector, UnknownHandler,
)
from .errors import ServiceError

DOMAINS = {
    'twitter': ('x.com', 'twitter.com', 'twimg.com'),
    'threads': ('threads.com', 'threads.net', 'instagram.com', 'cdninstagram.com', 'fbcdn.net'),
    'tiktok': ('tiktok.com', 'tiktokv.com', 'tiktokcdn.com', 'tiktokcdn-us.com',
               'tiktokv.us', 'byteoversea.com', 'ibytedtos.com', 'muscdn.com', 'musical.ly'),
    'youtube': ('youtube.com', 'youtu.be', 'youtube-nocookie.com', 'googlevideo.com',
                'googleapis.com', 'ytimg.com', 'ggpht.com', 'google.com'),
}
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


def checked_url(url, domains):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not any(host == domain or host.endswith('.' + domain) for domain in domains)):
            raise ValueError()
    except ValueError:
        raise ServiceError('UNSAFE_MEDIA_URL', 'The source returned an unsupported media address.')
    return host


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        if self._tunnel_host or self.port != 443:
            raise ServiceError('UNSAFE_MEDIA_URL', 'Proxy tunnels and custom ports are disabled.')
        records = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(r[4][0]) for r in records]
        if not addresses or any(not address.is_global or address.is_multicast or address.is_reserved for address in addresses):
            raise ServiceError('UNSAFE_ADDRESS', 'The source resolved to a non-public address.')
        last_error = None
        for family, kind, proto, _, address in records:
            connection = socket.socket(family, kind, proto)
            try:
                connection.settimeout(self.timeout)
                connection.connect(address)  # No second hostname lookup / DNS rebinding.
                self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
                return
            except OSError as error:
                connection.close()
                last_error = error
        raise last_error or OSError('No public endpoint available')


class PublicHTTPSHandler(HTTPSHandler):
    def __init__(self, domains):
        super().__init__(context=ssl.create_default_context())
        self.domains = domains

    def https_open(self, request):
        checked_url(request.full_url, self.domains)
        return self.do_open(PublicHTTPSConnection, request, context=self._context)


class PublicRedirectHandler(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def __init__(self, domains):
        self.domains = domains

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        checked_url(newurl, self.domains)
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected and urlsplit(request.full_url).hostname != urlsplit(newurl).hostname:
            for key in ('Authorization', 'Cookie', 'Host'):
                redirected.remove_header(key)
        return redirected


def public_opener(source, cookiejar=None):
    domains = DOMAINS[source]
    opener = OpenerDirector()
    for handler in (PublicHTTPSHandler(domains), PublicRedirectHandler(domains),
                    HTTPDefaultErrorHandler(), HTTPErrorProcessor(), UnknownHandler()):
        opener.add_handler(handler)
    if cookiejar is not None:
        opener.add_handler(HTTPCookieProcessor(cookiejar))
    opener.addheaders = [('User-Agent', USER_AGENT), ('Accept-Encoding', 'identity')]
    return opener


def read_limited(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ServiceError('RESPONSE_TOO_LARGE', 'The source response exceeded the preparation limit.')
    return data
