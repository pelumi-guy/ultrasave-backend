import io
import json
from pathlib import Path
import shutil
import socket
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from app.config import Settings
from app.errors import ServiceError
from app.extractor import select_formats, download_file, public_error, prepare
from app.network import DOMAINS, checked_url, PublicHTTPSConnection, PublicRedirectHandler
from app.threads import post_videos
from app.validation import validate_source_url


def fmt(url='https://video.twimg.com/video.mp4', **changes):
    return {'url': url, 'ext': 'mp4', 'protocol': 'https', 'vcodec': 'avc1.640028',
            'acodec': 'mp4a.40.2', 'height': 720, **changes}


@pytest.mark.parametrize('url', [
    'http://video.twimg.com/video.mp4', 'file:///etc/passwd',
    'https://video.twimg.com.evil.test/file', 'https://user:pass@video.twimg.com/file',
    'https://video.twimg.com:8443/file', 'https://127.0.0.1/file',
])
def test_untrusted_media_urls_are_blocked(url):
    with pytest.raises(ServiceError):
        checked_url(url, DOMAINS['twitter'])


@pytest.mark.parametrize('private', ['127.0.0.1', '169.254.169.254', '10.1.2.3', '::1', '::ffff:127.0.0.1', '224.0.0.1', 'ff02::1'])
def test_connection_rejects_mixed_public_and_private_dns(monkeypatch, private):
    records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
               (socket.AF_INET, socket.SOCK_STREAM, 6, '', (private, 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: records)
    connect = Mock()
    monkeypatch.setattr(socket, 'socket', connect)
    with pytest.raises(ServiceError, match='non-public'):
        PublicHTTPSConnection('video.twimg.com').connect()
    connect.assert_not_called()


def test_connection_pins_checked_ip_and_preserves_tls_hostname(monkeypatch):
    lookup = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    connection, tls = Mock(), Mock()
    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    monkeypatch.setattr(socket, 'socket', Mock(return_value=connection))
    request = PublicHTTPSConnection('video.twimg.com')
    request._context = tls
    request.connect()
    connection.connect.assert_called_once_with(('8.8.8.8', 443))
    tls.wrap_socket.assert_called_once_with(connection, server_hostname='video.twimg.com')
    lookup.assert_called_once()


def test_redirect_policy_rechecks_hosts_and_strips_sensitive_headers():
    handler = PublicRedirectHandler(DOMAINS['twitter'])
    request = Request('https://x.com/start', headers={'Authorization': 'secret', 'Cookie': 'private', 'Host': 'x.com'})
    result = handler.redirect_request(request, None, 302, '', {}, 'https://video.twimg.com/file')
    assert not result.has_header('Authorization') and not result.has_header('Cookie') and not result.has_header('Host')
    for target in ['http://x.com/end', 'https://evil.test/end', 'file:///tmp/file']:
        with pytest.raises(ServiceError):
            handler.redirect_request(request, None, 302, '', {}, target)


def test_youtube_chooses_h264_and_separate_aac_over_unsupported_codecs():
    muxed = fmt(height=360)
    video = fmt(acodec='none', height=1080)
    audio = fmt(ext='m4a', vcodec='none', acodec='mp4a.40.2', abr=128)
    av1 = fmt(vcodec='av01.0.08M.08', height=1080)
    assert select_formats({'formats': [muxed, video, audio, av1]}, 'youtube') == [video, audio]
    assert select_formats({'formats': [muxed, video]}, 'youtube') == [muxed]


def test_manifest_only_drm_audio_only_and_oversized_formats_are_not_selected():
    for formats in [[fmt(protocol='m3u8_native')], [fmt(has_drm=True)],
                    [fmt(height=2160)], [fmt(vcodec='none')], [fmt(url='file:///tmp/file')]]:
        with pytest.raises(ServiceError, match='compatible'):
            select_formats({'formats': formats}, 'twitter')


def test_download_checks_actual_bytes_not_just_content_length(tmp_path):
    class Response(io.BytesIO):
        headers = {}
    opener = Mock()
    opener.open.return_value = Response(b'x' * 100)
    with pytest.raises(ServiceError) as error:
        download_file(opener, fmt(), tmp_path / 'file.mp4', [0], 20)
    assert error.value.code == 'MEDIA_TOO_LARGE'


def test_incomplete_download_is_not_successful(tmp_path):
    class Response(io.BytesIO):
        headers = {'Content-Length': '100'}
    opener = Mock()
    opener.open.return_value = Response(b'x' * 10)
    with pytest.raises(ServiceError) as error:
        download_file(opener, fmt(), tmp_path / 'file.mp4', [0], 200)
    assert error.value.code == 'INCOMPLETE_MEDIA'


def script(data):
    return '<script type="application/json">' + json.dumps(data) + '</script>'


def test_threads_selects_exact_post_and_preserves_carousel_order():
    page = script({'data': [
        {'code': 'OTHER', 'video_versions': [{'url': 'https://cdn.fbcdn.net/wrong.mp4'}]},
        {'shortcode': 'TARGET', 'login_success': True},
        {'code': 'TARGET', 'carousel_media': [
            {'pk': 'first', 'video_versions': [{'url': 'https://cdn.fbcdn.net/1.mp4', 'height': 720}]},
            {'pk': 'image', 'image_versions2': {}},
            {'pk': 'second', 'video_versions': [{'url': 'https://cdn.fbcdn.net/2.mp4'}]},
        ]},
    ]})
    result = post_videos(page, 'TARGET')
    assert [item['id'] for item in result] == ['first', 'second']
    with pytest.raises(ServiceError) as error:
        post_videos(page, 'MISSING')
    assert error.value.code == 'CONTENT_UNAVAILABLE'


def test_threads_route_metadata_is_not_mistaken_for_a_post():
    with pytest.raises(ServiceError) as error:
        post_videos(script({'shortcode': 'TARGET', 'login_success': True}), 'TARGET')
    assert error.value.code == 'CONTENT_UNAVAILABLE'
    with pytest.raises(ServiceError) as error:
        post_videos(script({'code': 'TARGET', 'media_type': 1, 'image_versions2': {}}), 'TARGET')
    assert error.value.code == 'NO_MEDIA'


@pytest.mark.parametrize(('source', 'url'), [
    ('threads', 'https://www.threads.com/share/BAVaaLtd2V'),
    ('tiktok', 'https://vt.tiktok.com/ZSpxV2B5k/'),
    ('youtube', 'https://youtube.com/shorts/lL6swYbmAbI?si=share'),
])
def test_supplied_link_shapes_are_accepted(source, url):
    assert validate_source_url(source, url) == url


@pytest.mark.parametrize(('info', 'code'), [
    ({'is_live': True}, 'UNSUPPORTED_CONTENT'),
    ({'live_status': 'is_upcoming'}, 'UNSUPPORTED_CONTENT'),
    ({'has_drm': True}, 'UNSUPPORTED_CONTENT'),
    ({'duration': 901}, 'MEDIA_TOO_LONG'),
])
def test_content_limits_reject_before_downloading(monkeypatch, tmp_path, info, code):
    monkeypatch.setattr('app.extractor.ffmpeg_path', lambda: 'unused')
    monkeypatch.setattr('app.extractor.metadata', lambda *a: [{**info, 'formats': [fmt()]}])
    download = Mock()
    monkeypatch.setattr('app.extractor.download_file', download)
    with pytest.raises(ServiceError) as error:
        prepare('twitter', 'https://x.com/user/status/123', tmp_path,
                {'max_items': 20, 'max_duration': 900, 'max_bytes': 100000})
    assert error.value.code == code
    download.assert_not_called()


def test_real_local_mp4_remux_preserves_audio_and_checks_duration(monkeypatch, tmp_path):
    fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'ultrasave-sample.mp4'
    monkeypatch.setattr('app.extractor.metadata', lambda *a: [{'formats': [fmt()]}])
    def copy(_opener, _media, target, budget, _max_bytes):
        shutil.copyfile(fixture, target)
        budget[0] += fixture.stat().st_size
    monkeypatch.setattr('app.extractor.download_file', copy)
    items = prepare('twitter', 'https://x.com/user/status/123', tmp_path,
                    {'max_items': 20, 'max_duration': 900, 'max_bytes': 100000})
    assert len(items) == 1 and items[0]['has_audio'] is True and items[0]['duration'] > 0
    assert (tmp_path / items[0]['name']).stat().st_size == items[0]['bytes']
    assert not list(tmp_path.glob('input-*'))


def test_errors_do_not_expose_signed_urls_or_cookies():
    secret = 'https://video.twimg.com/file?token=secret Cookie: confidential'
    for error in [RuntimeError(secret), HTTPError(secret, 403, 'Forbidden', {}, None)]:
        result = public_error(error)
        assert 'secret' not in result['message'] and 'confidential' not in result['message']


def test_invalid_limits_fail_at_startup(tmp_path):
    with pytest.raises(RuntimeError, match='resource limits'):
        Settings(data_dir=tmp_path, max_media_bytes=0).initialize()
