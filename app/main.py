import hashlib
import hmac
import time
import uuid
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError
from .config import Settings, token_hash
from .database import database, Job
from .errors import ServiceError
from .validation import validate_source_url


class CreateJob(BaseModel):
    source: Literal['twitter', 'instagram', 'threads', 'tiktok', 'youtube']
    url: str = Field(default='', max_length=2048)
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r'^[a-zA-Z0-9_-]+$')
    fixture: Literal['mixed'] | None = None


class Selection(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=30)


def signature(settings, job_id, item_id, expires):
    return hmac.new(settings.signing_key, f'{job_id}:{item_id}:{expires}'.encode(), hashlib.sha256).hexdigest()


def create_app(settings: Settings | None = None):
    settings = (settings or Settings()).initialize()
    sessions = database(settings)
    app = FastAPI(title='UltraSave private API', version='0.1.0', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions, app.state.settings = sessions, settings

    @app.exception_handler(ServiceError)
    async def service_error(_request, error):
        return JSONResponse({'error': {'code': error.code, 'message': error.message}}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({'error': {'code': 'INVALID_REQUEST', 'message': 'The request contains invalid or missing fields.'}}, status_code=422)

    @app.middleware('http')
    async def request_limits(request: Request, call_next):
        if request.method in {'POST', 'PUT', 'PATCH'}:
            try:
                if int(request.headers.get('content-length', '0')) > 16384:
                    return JSONResponse({'error': {'code': 'REQUEST_TOO_LARGE', 'message': 'Request is too large.'}}, status_code=413)
            except ValueError:
                return JSONResponse({'error': {'code': 'INVALID_REQUEST', 'message': 'Invalid request length.'}}, status_code=400)
            # Also bound chunked bodies; never trust Content-Length alone.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse({'error': {'code': 'REQUEST_TOO_LARGE', 'message': 'Request is too large.'}}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def owner(request: Request):
        auth = request.headers.get('authorization', '')
        if not auth.startswith('Bearer ') or len(auth) > 512:
            raise ServiceError('UNAUTHORIZED', 'A valid device access token is required.', 401)
        digest = token_hash(auth[7:])
        if not any(hmac.compare_digest(digest, configured) for configured in settings.token_hashes):
            raise ServiceError('UNAUTHORIZED', 'This device access token is invalid or revoked.', 401)
        return digest

    def owned(session, job_id, user, allow_expired=False):
        job = session.get(Job, job_id)
        if not job or job.owner != user:
            raise ServiceError('NOT_FOUND', 'This job does not exist.', 404)
        if not allow_expired and job.expires <= time.time() and job.state not in {'completed', 'cancelled'}:
            raise ServiceError('EXPIRED', 'This job expired. Start a new request.', 410)
        return job

    def payload(job):
        expires = min(int(time.time()) + 900, int(job.expires))
        items = []
        for original in job.items:
            item = dict(original)
            item.pop('storage_name', None)
            if job.state == 'ready' and item['id'] in job.selected:
                item['url'] = f'{settings.public_url.rstrip("/")}/v1/artifacts/{job.id}/{item["id"]}?expires={expires}&signature={signature(settings, job.id, item["id"], expires)}'
            items.append(item)
        return {'id': job.id, 'state': job.state, 'demo': bool(job.fixture), 'items': items, 'error': job.error}

    @app.get('/health')
    def health():
        return {'status': 'ok', 'version': '0.1.0', 'live_extraction': bool(settings.live_sources)}

    @app.get('/v1/session')
    def session_status(user=Depends(owner)):
        return {'authenticated': True, 'fixtures': settings.fixtures_enabled,
                'live_extraction': bool(settings.live_sources), 'sources': sorted(settings.live_sources),
                'limits': {'bytes': settings.max_media_bytes, 'duration': settings.max_duration_seconds, 'items': settings.max_media_items}}

    @app.post('/v1/jobs', status_code=201)
    def create(body: CreateJob, user=Depends(owner)):
        if body.fixture and not settings.fixtures_enabled:
            raise ServiceError('FIXTURES_DISABLED', 'Sample downloads are disabled.', 403)
        if not body.fixture:
            validate_source_url(body.source, body.url)
            if body.source not in settings.live_sources:
                raise ServiceError('PROVIDER_NOT_READY', 'Live extraction for this source is not enabled on this backend.', 501)
        with sessions.begin() as session:
            # Serialize quota/idempotency checks per device across API processes.
            if session.bind.dialect.name == 'sqlite':
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            else:
                lock_id = int(user[:16], 16) % (2 ** 63)
                session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_id})
            existing = session.scalar(select(Job).where(Job.owner == user, Job.key == body.idempotency_key))
            if existing:
                if existing.source != body.source or existing.fixture != body.fixture or existing.url != body.url:
                    raise ServiceError('IDEMPOTENCY_CONFLICT', 'This request key was already used for different content.', 409)
                return payload(existing)
            now = time.time()
            daily = session.scalar(select(func.count()).select_from(Job).where(Job.owner == user, Job.created > now - 86400))
            active = session.scalar(select(func.count()).select_from(Job).where(Job.owner == user, Job.expires > now, Job.state.in_(['queued', 'resolving', 'awaiting_selection', 'ready'])))
            if daily >= settings.daily_jobs or active >= settings.active_jobs:
                raise ServiceError('QUOTA_EXCEEDED', 'This device has reached its active or daily job limit. Finish existing jobs or try later.', 429)
            job = Job(id=str(uuid.uuid4()), owner=user, key=body.idempotency_key, source=body.source, url=body.url, fixture=body.fixture, expires=now + settings.retention_seconds)
            session.add(job)
            try:
                session.flush()
            except IntegrityError:
                raise ServiceError('REQUEST_IN_PROGRESS', 'The same request is already being created. Retry with the same request key.', 409)
            return payload(job)

    @app.get('/v1/jobs/{job_id}')
    def get(job_id: str, user=Depends(owner)):
        with sessions() as session:
            return payload(owned(session, job_id, user))

    @app.get('/v1/requests/{key}')
    def find_request(key: str, user=Depends(owner)):
        # Side-effect-free recovery after a client loses the submission response.
        with sessions() as session:
            job = session.scalar(select(Job).where(Job.owner == user, Job.key == key))
            if not job:
                raise ServiceError('NOT_FOUND', 'This request has no server job.', 404)
            return {'id': job.id}

    @app.post('/v1/jobs/{job_id}/select')
    def select_items(job_id: str, body: Selection, user=Depends(owner)):
        with sessions.begin() as session:
            job = owned(session, job_id, user)
            if job.state not in {'awaiting_selection', 'ready'}:
                raise ServiceError('INVALID_STATE', 'This job is not ready for selection.', 409)
            selected = list(dict.fromkeys(body.item_ids))
            if not set(selected).issubset({item['id'] for item in job.items}):
                raise ServiceError('INVALID_SELECTION', 'The selection contains unknown media items.')
            if job.state == 'ready' and selected != job.selected:
                raise ServiceError('SELECTION_LOCKED', 'This job already has a different selection. Start a new request.', 409)
            job.selected, job.state = selected, 'ready'
            return payload(job)

    @app.post('/v1/jobs/{job_id}/ack')
    def acknowledge(job_id: str, body: Selection, user=Depends(owner)):
        with sessions.begin() as session:
            job = owned(session, job_id, user)
            if job.state not in {'ready', 'completed'}:
                raise ServiceError('INVALID_STATE', 'Only prepared files can be acknowledged.', 409)
            if not set(body.item_ids).issubset(set(job.selected)):
                raise ServiceError('INVALID_SELECTION', 'Only selected files can be acknowledged.')
            job.acknowledged = list(set(job.acknowledged) | set(body.item_ids))
            if set(job.acknowledged) == set(job.selected):
                job.state = 'completed'
            return payload(job)

    @app.post('/v1/jobs/{job_id}/cancel')
    def cancel(job_id: str, user=Depends(owner)):
        with sessions.begin() as session:
            job = owned(session, job_id, user, allow_expired=True)
            if job.state != 'completed':
                job.state = 'cancelled'
            return payload(job)

    @app.get('/v1/artifacts/{job_id}/{item_id}')
    def artifact(job_id: str, item_id: str, expires: int, signature: str):
        if expires < time.time() or expires > time.time() + 901 or not hmac.compare_digest(signature, globals()['signature'](settings, job_id, item_id, expires)):
            raise ServiceError('INVALID_SIGNATURE', 'This download link is invalid or expired.', 403)
        with sessions() as session:
            job = session.get(Job, job_id)
            if not job or job.state != 'ready' or job.expires <= time.time() or item_id not in job.selected:
                raise ServiceError('NOT_FOUND', 'This file is no longer available.', 404)
            item = next((item for item in job.items if item['id'] == item_id), None)
            if not item:
                raise ServiceError('NOT_FOUND', 'This file does not exist.', 404)
            root = (settings.data_dir / 'artifacts' / job.id).resolve()
            path = (root / item.get('storage_name', item['name'])).resolve()
            if path.parent != root or not path.is_file():
                raise ServiceError('NOT_FOUND', 'This file is no longer available.', 404)
            return FileResponse(path, media_type=item['mime'], filename=item['name'])

    return app
