import time
import uuid
from sqlalchemy import create_engine, String, Text, Float, Integer, JSON, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(120), index=True)
    source: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text, default='')
    fixture: Mapped[str | None] = mapped_column(String(20), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)
    expires: Mapped[float] = mapped_column(Float)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    lease_token: Mapped[str] = mapped_column(String(36), default='')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[list] = mapped_column(JSON, default=list)
    selected: Mapped[list] = mapped_column(JSON, default=list)
    acknowledged: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)


def database(settings):
    options = {'connect_args': {'check_same_thread': False, 'timeout': 15}} if settings.database_url.startswith('sqlite') else {}
    engine = create_engine(settings.database_url, **options)
    from sqlalchemy import UniqueConstraint
    # Idempotency is per device, enforced across API processes by the database.
    if not any(getattr(c, 'name', None) == 'uq_owner_key' for c in Job.__table__.constraints):
        Job.__table__.append_constraint(UniqueConstraint('owner', 'key', name='uq_owner_key'))
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def claim(sessions, lease_seconds=60):
    now = time.time()
    with sessions.begin() as session:
        candidate = session.scalar(select(Job).where(Job.expires > now, (Job.state == 'queued') | ((Job.state == 'resolving') & (Job.lease_until < now))).order_by(Job.created).limit(1))
        if not candidate:
            return None
        token = str(uuid.uuid4())
        result = session.execute(update(Job).where(Job.id == candidate.id, Job.state == candidate.state, Job.lease_until == candidate.lease_until).values(state='resolving', lease_until=now + lease_seconds, lease_token=token, attempts=Job.attempts + 1))
        return (candidate.id, token) if result.rowcount == 1 else None
