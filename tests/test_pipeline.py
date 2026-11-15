import hashlib
import time
from urllib.parse import urlsplit
import pytest
from fastapi.testclient import TestClient
from app.config import Settings, token_hash
from app.main import create_app
from app.database import Job
from app.worker import run_once, cleanup
from app.validation import validate_source_url, public_addresses
from app.errors import ServiceError

TOKEN = 'test-device-only-not-a-production-token'
OTHER = 'second-test-device'


@pytest.fixture
def environment(tmp_path):
    settings = Settings(data_dir=tmp_path, token_hashes={token_hash(TOKEN), token_hash(OTHER)}, signing_key=b'test-key-never-use-outside-tests', public_url='http://testserver')
    app = create_app(settings)
    return settings, app.state.sessions, TestClient(app)


def headers(token=TOKEN):
    return {'Authorization': f'Bearer {token}'}


def create(client, key='request-key-0001', **changes):
    body = {'source': 'twitter', 'url': '', 'fixture': 'mixed', 'idempotency_key': key, **changes}
    return client.post('/v1/jobs', json=body, headers=headers())


def path(url):
    parsed = urlsplit(url)
    return parsed.path + '?' + parsed.query


def ready(environment):
    settings, sessions, client = environment
    response = create(client)
    assert response.status_code == 201, response.text
    job_id = response.json()['id']
    assert run_once(settings, sessions)
    result = client.get(f'/v1/jobs/{job_id}', headers=headers()).json()
    assert result['state'] == 'awaiting_selection'
    assert [item['kind'] for item in result['items']] == ['image', 'video']
    result = client.post(f'/v1/jobs/{job_id}/select', json={'item_ids': ['image', 'video']}, headers=headers())
    assert result.status_code == 200
    return job_id, result.json()


def test_auth_and_owner_isolation(environment):
    _, _, client = environment
    assert client.get('/v1/session').status_code == 401
    assert client.get('/v1/session', headers=headers('wrong')).status_code == 401
    job_id = create(client).json()['id']
    assert client.get(f'/v1/jobs/{job_id}', headers=headers(OTHER)).status_code == 404
    assert client.post(f'/v1/jobs/{job_id}/cancel', headers=headers(OTHER)).status_code == 404


def test_lost_submission_response_can_be_found_and_cancelled_without_creating_jobs(environment):
    settings, sessions, client = environment
    job_id = create(client, key='lost-response-key').json()['id']
    assert client.get('/v1/requests/lost-response-key').status_code == 401
    assert client.get('/v1/requests/lost-response-key', headers=headers(OTHER)).status_code == 404
    assert client.get('/v1/requests/lost-response-key', headers=headers()).json() == {'id': job_id}
    assert client.get('/v1/requests/never-submitted', headers=headers()).status_code == 404
    assert client.post(f'/v1/jobs/{job_id}/cancel', headers=headers()).json()['state'] == 'cancelled'
    assert not run_once(settings, sessions)


def test_full_lifecycle_selection_signed_transfer_partial_ack_and_cleanup(environment):
    settings, sessions, client = environment
    job_id, result = ready(environment)
    image, video = result['items']
    response = client.get(path(image['url']))
    assert response.status_code == 200 and response.content.startswith(b'\x89PNG')
    response = client.get(path(video['url']), headers={'Range': 'bytes=0-31'})
    assert response.status_code == 206 and len(response.content) == 32
    assert b'ftyp' in response.content
    wrong = client.post(f'/v1/jobs/{job_id}/ack', json={'item_ids': ['unknown']}, headers=headers())
    assert wrong.status_code == 400
    partial = client.post(f'/v1/jobs/{job_id}/ack', json={'item_ids': ['image']}, headers=headers()).json()
    assert partial['state'] == 'ready'
    cleanup(settings, sessions)
    assert client.get(path(video['url'])).status_code == 200
    completed = client.post(f'/v1/jobs/{job_id}/ack', json={'item_ids': ['video']}, headers=headers()).json()
    assert completed['state'] == 'completed'
    cleanup(settings, sessions)
    assert not (settings.data_dir / 'artifacts' / job_id).exists()


def test_expired_job_can_still_be_cancelled(environment):
    settings, sessions, client = environment
    job_id = create(client).json()['id']
    with sessions.begin() as session:
        session.get(Job, job_id).expires = time.time() - 1
    assert client.post(f'/v1/jobs/{job_id}/cancel', headers=headers()).json()['state'] == 'cancelled'
    cleanup(settings, sessions)


def test_terminal_metadata_retention(environment):
    settings, sessions, client = environment
    job_id = create(client).json()['id']
    with sessions.begin() as session:
        job = session.get(Job, job_id)
        job.state, job.created = 'cancelled', time.time() - 8 * 86400
    cleanup(settings, sessions)
    assert client.get(f'/v1/jobs/{job_id}', headers=headers()).status_code == 404


def test_concurrent_quota_is_atomic(environment):
    from concurrent.futures import ThreadPoolExecutor
    settings, _, client = environment
    settings.active_jobs = 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda key: create(client, key=key).status_code, ['parallel-key-one', 'parallel-key-two']))
    assert sorted(statuses) == [201, 429]


def test_idempotency_and_quota(environment):
    settings, _, client = environment
    settings.active_jobs = 1
    first = create(client)
    second = create(client)
    assert first.json()['id'] == second.json()['id']
    assert create(client, source='instagram').status_code == 409
    assert create(client, key='different-key').status_code == 429


def test_invalid_selection_and_state(environment):
    _, _, client = environment
    job_id = create(client).json()['id']
    assert client.post(f'/v1/jobs/{job_id}/select', json={'item_ids': ['image']}, headers=headers()).status_code == 409
    assert client.post(f'/v1/jobs/{job_id}/ack', json={'item_ids': ['image']}, headers=headers()).status_code == 409


def test_signature_tamper_cancel_and_retention(environment):
    settings, sessions, client = environment
    job_id, result = ready(environment)
    url = path(result['items'][0]['url'])
    assert client.get(url[:-1] + ('0' if url[-1] != '0' else '1')).status_code == 403
    assert client.post(f'/v1/jobs/{job_id}/cancel', headers=headers()).json()['state'] == 'cancelled'
    assert client.get(url).status_code == 404
    cleanup(settings, sessions)
    assert not (settings.data_dir / 'artifacts' / job_id).exists()


def test_stale_worker_lease_is_recovered(environment):
    settings, sessions, client = environment
    job_id = create(client).json()['id']
    with sessions.begin() as session:
        job = session.get(Job, job_id)
        job.state, job.lease_until = 'resolving', time.time() - 1
    assert run_once(settings, sessions)
    assert client.get(f'/v1/jobs/{job_id}', headers=headers()).json()['state'] == 'awaiting_selection'


def test_expiry_cleanup(environment):
    settings, sessions, client = environment
    job_id, result = ready(environment)
    with sessions.begin() as session:
        session.get(Job, job_id).expires = time.time() - 1
    assert client.get(f'/v1/jobs/{job_id}', headers=headers()).status_code == 410
    assert client.get(path(result['items'][0]['url'])).status_code == 404
    cleanup(settings, sessions)
    assert not (settings.data_dir / 'artifacts' / job_id).exists()


def test_live_extraction_is_explicit_and_does_not_fetch(environment):
    settings, _, client = environment
    settings.live_sources = frozenset()
    assert create(client, fixture=None, url='https://x.com/user/status/123').status_code == 501
    assert create(client, fixture=None, url='https://x.com.evil.test/user/status/123').status_code == 400
    assert create(client, fixture=None, url='https://x.com/user').status_code == 400


def test_live_job_uses_existing_selection_and_signed_artifact_flow(environment, monkeypatch):
    from app.providers import FixtureProvider
    settings, sessions, client = environment
    calls = []
    def resolve(provider, url, destination):
        calls.append((provider.source, url))
        return FixtureProvider().resolve('', destination)
    monkeypatch.setattr('app.worker.ExtractionProvider.resolve', resolve)
    response = create(client, fixture=None, url='https://x.com/user/status/123')
    assert response.status_code == 201
    job_id = response.json()['id']
    assert response.json()['demo'] is False
    assert run_once(settings, sessions)
    assert calls == [('twitter', 'https://x.com/user/status/123')]
    result = client.get(f'/v1/jobs/{job_id}', headers=headers()).json()
    assert result['state'] == 'awaiting_selection'
    assert all('storage_name' not in item for item in result['items'])
    assert result['items'][0]['name'] == 'ultrasave-sample.png'
    selected = client.post(f'/v1/jobs/{job_id}/select', json={'item_ids': ['video']}, headers=headers()).json()
    media = next(item for item in selected['items'] if item['id'] == 'video')
    assert client.get(path(media['url'])).content[4:8] == b'ftyp'
    assert client.get('/v1/session', headers=headers()).json()['sources'] == ['threads', 'tiktok', 'twitter', 'youtube']


def test_live_failure_is_typed_and_partial_files_are_cleaned(environment, monkeypatch):
    settings, sessions, client = environment
    def fail(provider, url, directory):
        directory.mkdir(parents=True)
        (directory / 'partial.mp4').write_bytes(b'incomplete')
        raise ServiceError('SOURCE_RATE_LIMITED', 'Try later.')
    monkeypatch.setattr('app.worker.ExtractionProvider.resolve', fail)
    job_id = create(client, fixture=None, url='https://x.com/user/status/123').json()['id']
    assert run_once(settings, sessions)
    result = client.get(f'/v1/jobs/{job_id}', headers=headers()).json()
    assert result['state'] == 'failed' and result['error']['code'] == 'SOURCE_RATE_LIMITED'
    assert not list((settings.data_dir / 'artifacts' / job_id).rglob('*.mp4'))


def test_cancel_during_preparation_does_not_publish_files(environment, monkeypatch):
    from app.providers import FixtureProvider
    settings, sessions, client = environment
    job_id = create(client, fixture=None, url='https://x.com/user/status/123').json()['id']
    def resolve(provider, url, directory):
        assert provider.should_continue()
        client.post(f'/v1/jobs/{job_id}/cancel', headers=headers())
        assert not provider.should_continue()
        return FixtureProvider().resolve('', directory)
    monkeypatch.setattr('app.worker.ExtractionProvider.resolve', resolve)
    assert run_once(settings, sessions)
    result = client.get(f'/v1/jobs/{job_id}', headers=headers()).json()
    assert result['state'] == 'cancelled' and result['items'] == []
    assert not list((settings.data_dir / 'artifacts' / job_id).rglob('*.mp4'))


def test_stale_attempt_cannot_delete_new_attempt_files(environment, monkeypatch):
    from app.providers import FixtureProvider
    settings, sessions, client = environment
    job_id = create(client, fixture=None, url='https://x.com/user/status/123').json()['id']
    sentinel = settings.data_dir / 'artifacts' / job_id / 'new-attempt.mp4'
    def resolve(provider, url, directory):
        result = FixtureProvider().resolve('', directory)
        sentinel.write_bytes(b'new attempt owns this file')
        with sessions.begin() as session:
            session.get(Job, job_id).lease_token = 'a-new-worker-lease'
        return result
    monkeypatch.setattr('app.worker.ExtractionProvider.resolve', resolve)
    assert run_once(settings, sessions)
    assert sentinel.read_bytes() == b'new attempt owns this file'
    assert client.get(f'/v1/jobs/{job_id}', headers=headers()).json()['state'] == 'resolving'


def test_request_size_and_fixture_disable(environment):
    settings, _, client = environment
    assert client.post('/v1/jobs', content=b'x' * 17000, headers=headers()).status_code == 413
    settings.fixtures_enabled = False
    assert create(client).status_code == 403


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1', '::ffff:127.0.0.1'])
def test_nonpublic_dns_rejected(address):
    with pytest.raises(ServiceError):
        public_addresses('example.com', lambda *args, **kwargs: [(None, None, None, None, (address, 443))])


@pytest.mark.parametrize(('source', 'url'), [
    ('twitter', 'https://x.com/user/status/123'), ('instagram', 'https://instagram.com/p/ABC123/'),
    ('threads', 'https://threads.com/@user/post/ABC123'), ('tiktok', 'https://vm.tiktok.com/ABC123/'),
    ('youtube', 'https://youtu.be/abcdefghijk')])
def test_source_contract(source, url):
    assert validate_source_url(source, url) == url
