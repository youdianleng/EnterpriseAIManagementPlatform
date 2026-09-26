"""PostgreSQL `ltree` support for SQLAlchemy.

Neither SQLAlchemy nor pgvector ships an `ltree` type. Declaring the column as
`Text` would work at runtime because psycopg round-trips ltree as a string, but
it would make Alembic autogenerate believe the column differs from the model and
propose a bogus ALTER on every run. A small UserDefinedType avoids that.
"""

from sqlalchemy.types import UserDefinedType


class Ltree(UserDefinedType):
    """The `ltree` label-path type.

    Values are exchanged as plain strings (`"company.engineering.backend"`),
    which is also the form the queries in `repositories/org.py` use when casting
    a parameter with `CAST(:path AS ltree)`.
    """

    cache_ok = True

    def get_col_spec(self, **_: object) -> str:
        return "ltree"

    def bind_processor(self, dialect: object):  # noqa: ANN201 - SQLAlchemy hook signature
        def process(value: str | None) -> str | None:
            return value

        return process

    def result_processor(self, dialect: object, coltype: object):  # noqa: ANN201
        def process(value: str | None) -> str | None:
            return value

        return process
