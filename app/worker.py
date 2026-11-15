import logging
import shutil
import time
from sqlalchemy import select, update, delete
from .config import Settings
from .database import database, claim, Job
from .errors import ServiceError
from .providers import FixtureProvider, ExtractionProvider

logger = logging.getLogger('ultrasave.worker')


def cleanup(settings, sessions):
    now = time.time()
    with sessions.begin() as session:
        jobs = session.scalars(select(Job).where((Job.expires <= now) | Job.state.in_(['cancelled', 'completed']))).all()
        for job in jobs:
            shutil.rmtree(settings.data_dir / 'artifacts' / job.id, ignore_errors=True)
            if job.state not in {'cancelled', 'completed'}:
                job.state = 'expired'
        # Remove abandoned artifact directories left by a worker crash before commit.
        root = settings.data_dir / 'artifacts'
        if root.exists():
            for path in root.iterdir():
                if path.is_dir() and path.stat().st_mtime < now - settings.retention_seconds:
                    shutil.rmtree(path, ignore_errors=True)
        session.execute(delete(Job).where(Job.created < now - 7 * 86400, Job.state.in_(['completed', 'cancelled', 'expired', 'failed'])))


def run_once(settings, sessions):
    cleanup(settings, sessions)
    claimed = claim(sessions, lease_seconds=settings.preparation_timeout + 45)
    if not claimed:
        return False
    job_id, lease_token = claimed
    root = settings.data_dir / 'artifacts' / job_id
    directory = root / lease_token
    published = []
    def should_continue():
        with sessions() as session:
            current = session.get(Job, job_id)
            return bool(current and current.state == 'resolving' and current.lease_token == lease_token
                        and current.expires > time.time())
    try:
        with sessions() as session:
            job = session.get(Job, job_id)
            if job.attempts > 3:
                raise ServiceError('WORKER_RETRIES_EXHAUSTED', 'The preparation worker could not complete this request.', 503)
            source, url, fixture = job.source, job.url, job.fixture
        provider = FixtureProvider() if fixture else ExtractionProvider(source, settings, should_continue)
        items = provider.resolve(url, directory)
        with sessions.begin() as session:
            # Claim ownership before publishing; stale attempts only remove their own staging.
            result = session.execute(update(Job).where(Job.id == job_id, Job.state == 'resolving', Job.lease_token == lease_token,
                                                       Job.expires > time.time()).values(state='awaiting_selection', lease_until=0))
            if result.rowcount != 1:
                shutil.rmtree(directory, ignore_errors=True)
                return True
            for item in items:
                original = directory / item['name']
                if original.parent != directory or not original.is_file() or original.stat().st_size != item['bytes']:
                    raise ServiceError('INVALID_MEDIA', 'Prepared media could not be verified.')
                name = item['name'] if fixture else f'{lease_token}-{item["name"]}'
                original.replace(root / name)
                published.append(root / name)
                item['storage_name'] = name
            session.get(Job, job_id).items = items
        shutil.rmtree(directory, ignore_errors=True)
    except Exception as error:
        shutil.rmtree(directory, ignore_errors=True)
        for path in published:
            path.unlink(missing_ok=True)
        with sessions.begin() as session:
            current = session.get(Job, job_id)
            if current and current.state == 'resolving' and current.lease_token == lease_token:
                current.state = 'failed'
                current.error = {'code': error.code, 'message': error.message} if isinstance(error, ServiceError) else {'code': 'PREPARATION_FAILED', 'message': 'The worker could not prepare this collection.'}
                current.lease_until = 0
    return True


def main():
    settings = Settings().initialize()
    sessions = database(settings)
    logging.basicConfig(level=logging.INFO)
    logger.info('Worker ready. Public-video adapters enabled: %s', ', '.join(sorted(settings.live_sources)) or 'none')
    while True:
        try:
            if not run_once(settings, sessions):
                time.sleep(1)
        except Exception:
            logger.error('Worker cycle failed; retrying without logging source URLs or credentials.')
            time.sleep(2)


if __name__ == '__main__':
    main()
