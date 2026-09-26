"""Declarative metadata for Alembic autogenerate.

Tables arrive with the feature tickets (06, 07, 09 ...), so this is the empty
base they attach to. Keeping it separate from `app/db.py` lets Alembic import
model metadata without constructing an engine.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for every ORM model."""


# Import model modules here as they are added so autogenerate can see them.
MODEL_MODULES: tuple[str, ...] = ()

metadata = Base.metadata
