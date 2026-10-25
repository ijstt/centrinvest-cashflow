from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from src.config import settings

from sqlalchemy.pool import NullPool

Base = declarative_base()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    poolclass=NullPool,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db():
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        def upgrade_snapshots(connection):
            from sqlalchemy import inspect
            if 'calculation_state' not in {c['name'] for c in inspect(connection).get_columns('forecast_snapshots')}:
                connection.exec_driver_sql('ALTER TABLE forecast_snapshots ADD COLUMN calculation_state JSON')
        await conn.run_sync(upgrade_snapshots)
