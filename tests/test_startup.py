import json
import sys

import pytest
from fastapi.testclient import TestClient

from app import manage
from app.config import Settings, token_hash
from app.main import create_app


@pytest.fixture
def startup_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('ULTRASAVE_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('ULTRASAVE_DATABASE_URL', f'sqlite:///{(tmp_path / "jobs.db").as_posix()}')
    monkeypatch.setenv('ULTRASAVE_LIVE_SOURCES', '')
    monkeypatch.setattr(sys, 'argv', ['app.manage', 'init'])
    return tmp_path


def test_fresh_volume_initialization_allows_api_startup(startup_directory, capsys):
    with pytest.raises(RuntimeError, match='No device tokens configured'):
        Settings().initialize()

    manage.main()
    settings = Settings().initialize()
    token = (startup_directory / 'device-token.txt').read_text(encoding='utf-8')
    assert settings.token_hashes == {token_hash(token)}
    assert len(settings.signing_key) == 32
    assert token not in capsys.readouterr().out

    app = create_app(settings)
    try:
        with TestClient(app) as client:
            assert client.get('/health').status_code == 200
            assert client.get('/v1/session').status_code == 401
            assert client.get('/v1/session', headers={'Authorization': f'Bearer {token}'}).status_code == 200
    finally:
        app.state.sessions.kw['bind'].dispose()


def test_redeployment_preserves_credentials_without_plaintext_token(startup_directory):
    manage.main()
    settings = Settings().initialize()
    (startup_directory / 'device-token.txt').unlink()

    manage.main()
    reloaded = Settings().initialize()
    assert reloaded.token_hashes == settings.token_hashes
    assert reloaded.signing_key == settings.signing_key
    assert not (startup_directory / 'device-token.txt').exists()


def test_initialization_retries_without_changing_prepared_credentials(startup_directory, monkeypatch):
    write_atomic = manage.write_atomic

    def fail_before_hash_commit(path, data):
        if path.name == 'token-hashes.json':
            raise OSError('Simulated interrupted initialization')
        write_atomic(path, data)

    monkeypatch.setattr(manage, 'write_atomic', fail_before_hash_commit)
    with pytest.raises(OSError, match='Simulated interrupted initialization'):
        manage.main()
    assert not (startup_directory / 'token-hashes.json').exists()
    token = (startup_directory / 'device-token.txt').read_bytes()
    key = (startup_directory / 'signing-key').read_bytes()

    monkeypatch.setattr(manage, 'write_atomic', write_atomic)
    manage.main()
    assert (startup_directory / 'device-token.txt').read_bytes() == token
    assert (startup_directory / 'signing-key').read_bytes() == key
    assert json.loads((startup_directory / 'token-hashes.json').read_text()) == [token_hash(token.decode())]
    Settings().initialize()


def test_incomplete_existing_configuration_fails_before_services_start(startup_directory):
    manage.main()
    hashes = (startup_directory / 'token-hashes.json').read_bytes()
    (startup_directory / 'signing-key').unlink()

    with pytest.raises(RuntimeError, match='Signing key missing'):
        manage.main()
    assert (startup_directory / 'token-hashes.json').read_bytes() == hashes
    assert not (startup_directory / 'signing-key').exists()
