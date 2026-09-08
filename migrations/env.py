import os

from alembic import context
from sqlalchemy import create_engine, pool

from fair.schemas.db import Base

url = os.environ.get("FAIR_DATABASE_URL", "postgresql+psycopg://fair:fair@localhost:5432/fair")
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
