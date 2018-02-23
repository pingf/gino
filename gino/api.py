import sys
import weakref

import sqlalchemy as sa
from asyncpg import Connection
from sqlalchemy.sql.base import Executable
from sqlalchemy.dialects import postgresql as sa_pg

from .connection import GinoConnection
from .crud import CRUDModel
from .declarative import declarative_base
from .dialects.asyncpg import GinoCursorFactory
from .pool import GinoPool
from . import json_support


class GinoTransaction:
    __slots__ = ('_conn_ctx', '_isolation', '_readonly', '_deferrable', '_ctx')

    def __init__(self, conn_ctx, isolation, readonly, deferrable):
        self._conn_ctx = conn_ctx
        self._isolation = isolation
        self._readonly = readonly
        self._deferrable = deferrable
        self._ctx = None

    async def __aenter__(self):
        conn = await self._conn_ctx.__aenter__()
        try:
            self._ctx = conn.transaction(isolation=self._isolation,
                                         readonly=self._readonly,
                                         deferrable=self._deferrable)
            return conn, await self._ctx.__aenter__()
        except Exception:
            await self._conn_ctx.__aexit__(*sys.exc_info())
            raise

    async def __aexit__(self, extype, ex, tb):
        try:
            await self._ctx.__aexit__(extype, ex, tb)
        except Exception:
            await self._conn_ctx.__aexit__(*sys.exc_info())
            raise
        else:
            await self._conn_ctx.__aexit__(extype, ex, tb)


class ConnectionAcquireContext:
    __slots__ = ('_connection',)

    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def __await__(self):
        return self

    def __next__(self):
        raise StopIteration(self._connection)


class BindContext:
    def __init__(self, bind):
        self._bind = bind
        self._ctx = None

    async def __aenter__(self):
        args = {}
        if isinstance(self._bind, Connection):
            return self._bind
        elif isinstance(self._bind, GinoPool):
            args = dict(reuse=True)
        # noinspection PyArgumentList
        self._ctx = self._bind.acquire(**args)
        return await self._ctx.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            await ctx.__aexit__(exc_type, exc_val, exc_tb)


class GinoExecutor:
    __slots__ = ('_query',)

    def __init__(self, query):
        self._query = query

    @property
    def query(self):
        return self._query

    def model(self, model):
        self._query = self._query.execution_options(model=weakref.ref(model))
        return self

    def return_model(self, switch):
        self._query = self._query.execution_options(return_model=switch)
        return self

    def timeout(self, timeout):
        self._query = self._query.execution_options(timeout)
        return self

    async def all(self, *multiparams, bind=None, **params):
        if bind is None:
            bind = self._query.bind
        return await bind.all(self._query, *multiparams, **params)

    async def first(self, *multiparams, bind=None, **params):
        if bind is None:
            bind = self._query.bind
        return await bind.first(self._query, *multiparams, **params)

    async def scalar(self, *multiparams, bind=None, **params):
        if bind is None:
            bind = self._query.bind
        return await bind.scalar(self._query, *multiparams, **params)

    async def status(self, *multiparams, bind=None, **params):
        if bind is None:
            bind = self._query.bind
        return await bind.status(self._query, *multiparams, **params)

    def iterate(self, *multiparams, connection=None, **params):
        def env_factory():
            conn = connection or self._query.bind
            return conn, conn.metadata
        return GinoCursorFactory(env_factory, self._query, multiparams, params)


class Gino(sa.MetaData):
    model_base_classes = (CRUDModel,)
    query_executor = GinoExecutor
    connection_cls = GinoConnection
    pool_cls = GinoPool

    def __init__(self, bind=None, model_classes=None,
                 query_ext=True, **kwargs):
        super().__init__(bind=bind, **kwargs)
        if model_classes is None:
            model_classes = self.model_base_classes
        self.Model = declarative_base(self, model_classes)
        for mod in json_support, sa_pg, sa:
            for key in mod.__all__:
                if not hasattr(self, key):
                    setattr(self, key, getattr(mod, key))
        if query_ext:
            Executable.gino = property(self.query_executor)

    async def create_engine(self, name_or_url, loop=None, **kwargs):
        from .strategies import create_engine
        e = await create_engine(name_or_url, loop=loop, **kwargs)
        self.bind = e
        return e

    async def dispose_engine(self):
        if self.bind is not None:
            bind, self.bind = self.bind, None
            await bind.close()

    def compile(self, elem, *multiparams, **params):
        return self.bind.compile(elem, *multiparams, **params)

    async def all(self, clause, *multiparams, **params):
        return await self.bind.all(clause, *multiparams, **params)

    async def first(self, clause, *multiparams, **params):
        return await self.bind.first(clause, *multiparams, **params)

    async def scalar(self, clause, *multiparams, **params):
        return await self.bind.scalar(clause, *multiparams, **params)

    async def status(self, clause, *multiparams, **params):
        return await self.bind.status(clause, *multiparams, **params)

    def iterate(self, clause, *multiparams, connection=None, **params):
        return GinoCursorFactory(lambda: (connection or self.bind, self),
                                 clause, multiparams, params)

    def acquire(self, *, timeout=None, reuse=True, lazy=False):
        method = getattr(self._bind, 'acquire', None)
        if method is None:
            return ConnectionAcquireContext(self._bind)
        else:
            return method(timeout=timeout, reuse=reuse, lazy=lazy)

    def transaction(self, *, isolation='read_committed', readonly=False,
                    deferrable=False, timeout=None, reuse=True):
        return GinoTransaction(self.acquire(timeout=timeout, reuse=reuse),
                               isolation, readonly, deferrable)
