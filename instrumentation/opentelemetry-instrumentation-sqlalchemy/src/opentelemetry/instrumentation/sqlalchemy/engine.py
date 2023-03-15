# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import re

from sqlalchemy.event import (  # pylint: disable=no-name-in-module
    listen,
    remove,
)

from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy.version import __version__
from opentelemetry.instrumentation.sqlcommenter_utils import _add_sql_comment
from opentelemetry.instrumentation.utils import _get_opentelemetry_values
from opentelemetry.semconv.trace import NetTransportValues, SpanAttributes
from opentelemetry.trace.status import Status, StatusCode

sql_reserved_words = [
    "ADD",
    "ALL",
    "ALTER",
    "AND",
    "ANY",
    "AS",
    "ASC",
    "BACKUP",
    "BETWEEN",
    "CASE",
    "CHECK",
    "COLUMN",
    "CONSTRAINT",
    "CREATE",
    "DATABASE",
    "DEFAULT",
    "DELETE",
    "DESC",
    "DISTINCT",
    "DROP",
    "EXEC",
    "EXISTS",
    "FOREIGN",
    "FROM",
    "FULL",
    "GROUP",
    "BY",
    "HAVING",
    "IN",
    "INDEX",
    "INNER",
    "INSERT",
    "INTO",
    "IS",
    "JOIN",
    "KEY",
    "LEFT",
    "LIKE",
    "LIMIT",
    "NOT",
    "NULL",
    "ON",
    "OR",
    "ORDER",
    "OUTER",
    "PRIMARY",
    "PROCEDURE",
    "RIGHT",
    "ROWNUM",
    "SELECT",
    "SET",
    "TABLE",
    "TOP",
    "TRUNCATE",
    "UNION",
    "UNIQUE",
    "UPDATE",
    "VALUES",
    "VIEW",
    "WHERE",
    "=",
]

sql_reserved_dict = {word: True for word in sql_reserved_words}


def _sanitize_query(query):
    """Remove query content, replace with sanitization symbol.
    For example `SELECT * FROM table` will sanitize to SELECT ? FROM ?`
    """
    sanitized_query = ""
    if not query:
        return sanitized_query

    for word in query.split():
        if word.upper() not in sql_reserved_dict:
            word = "?"
        sanitized_query += word + " "
    return sanitized_query.strip()


def _normalize_vendor(vendor):
    """Return a canonical name for a type of database."""
    if not vendor:
        return "db"  # should this ever happen?

    if "sqlite" in vendor:
        return "sqlite"

    if "postgres" in vendor or vendor == "psycopg2":
        return "postgresql"

    return vendor


def _wrap_create_async_engine(
    tracer, connections_usage, sanitize_query, enable_commenter=False
):
    # pylint: disable=unused-argument
    def _wrap_create_async_engine_internal(func, module, args, kwargs):
        """Trace the SQLAlchemy engine, creating an `EngineTracer`
        object that will listen to SQLAlchemy events.
        """
        engine = func(*args, **kwargs)
        EngineTracer(
            tracer,
            engine.sync_engine,
            connections_usage,
            sanitize_query,
            enable_commenter,
        )
        return engine

    return _wrap_create_async_engine_internal


def _wrap_create_engine(
    tracer, connections_usage, sanitize_query, enable_commenter=False
):
    def _wrap_create_engine_internal(func, _module, args, kwargs):
        """Trace the SQLAlchemy engine, creating an `EngineTracer`
        object that will listen to SQLAlchemy events.
        """
        engine = func(*args, **kwargs)
        EngineTracer(
            tracer, engine, connections_usage, sanitize_query, enable_commenter
        )
        return engine

    return _wrap_create_engine_internal


def _wrap_connect(tracer):
    # pylint: disable=unused-argument
    def _wrap_connect_internal(func, module, args, kwargs):
        with tracer.start_as_current_span(
            "connect", kind=trace.SpanKind.CLIENT
        ) as span:
            if span.is_recording():
                attrs, _ = _get_attributes_from_url(module.url)
                span.set_attributes(attrs)
                span.set_attribute(
                    SpanAttributes.DB_SYSTEM, _normalize_vendor(module.name)
                )
            return func(*args, **kwargs)

    return _wrap_connect_internal


class EngineTracer:
    _remove_event_listener_params = []

    def __init__(
        self,
        tracer,
        engine,
        connections_usage,
        sanitize_query=False,
        enable_commenter=False,
        commenter_options=None,
    ):
        self.tracer = tracer
        self.engine = engine
        self.connections_usage = connections_usage
        self.vendor = _normalize_vendor(engine.name)
        self.enable_commenter = enable_commenter
        self.commenter_options = commenter_options if commenter_options else {}
        self.sanitize_query = sanitize_query
        self._leading_comment_remover = re.compile(r"^/\*.*?\*/")

        self._register_event_listener(
            engine, "before_cursor_execute", self._before_cur_exec, retval=True
        )
        self._register_event_listener(
            engine, "after_cursor_execute", _after_cur_exec
        )
        self._register_event_listener(engine, "handle_error", _handle_error)
        self._register_event_listener(engine, "connect", self._pool_connect)
        self._register_event_listener(engine, "close", self._pool_close)
        self._register_event_listener(engine, "checkin", self._pool_checkin)
        self._register_event_listener(engine, "checkout", self._pool_checkout)

    def _get_pool_name(self):
        return self.engine.pool.logging_name or ""

    def _add_idle_to_connection_usage(self, value):
        self.connections_usage.add(
            value,
            attributes={
                "pool.name": self._get_pool_name(),
                "state": "idle",
            },
        )

    def _add_used_to_connection_usage(self, value):
        self.connections_usage.add(
            value,
            attributes={
                "pool.name": self._get_pool_name(),
                "state": "used",
            },
        )

    def _pool_connect(self, _dbapi_connection, _connection_record):
        self._add_idle_to_connection_usage(1)

    def _pool_close(self, _dbapi_connection, _connection_record):
        self._add_idle_to_connection_usage(-1)

    # Called when a connection returns to the pool.
    def _pool_checkin(self, _dbapi_connection, _connection_record):
        self._add_used_to_connection_usage(-1)
        self._add_idle_to_connection_usage(1)

    # Called when a connection is retrieved from the Pool.
    def _pool_checkout(
        self, _dbapi_connection, _connection_record, _connection_proxy
    ):
        self._add_idle_to_connection_usage(-1)
        self._add_used_to_connection_usage(1)

    @classmethod
    def _register_event_listener(cls, target, identifier, func, *args, **kw):
        listen(target, identifier, func, *args, **kw)
        cls._remove_event_listener_params.append((target, identifier, func))

    @classmethod
    def remove_all_event_listeners(cls):
        for remove_params in cls._remove_event_listener_params:
            remove(*remove_params)
        cls._remove_event_listener_params.clear()

    def _operation_name(self, db_name, statement):
        parts = []
        if isinstance(statement, str):
            # otel spec recommends against parsing SQL queries. We are not trying to parse SQL
            # but simply truncating the statement to the first word. This covers probably >95%
            # use cases and uses the SQL statement in span name correctly as per the spec.
            # For some very special cases it might not record the correct statement if the SQL
            # dialect is too weird but in any case it shouldn't break anything.
            # Strip leading comments so we get the operation name.
            parts.append(
                self._leading_comment_remover.sub("", statement).split()[0]
            )
        if db_name:
            parts.append(db_name)
        if not parts:
            return self.vendor
        return " ".join(parts)

    def _before_cur_exec(
        self, conn, cursor, statement, params, context, _executemany
    ):
        attrs, found = _get_attributes_from_url(conn.engine.url)
        if not found:
            attrs = _get_attributes_from_cursor(self.vendor, cursor, attrs)

        db_name = attrs.get(SpanAttributes.DB_NAME, "")
        span = self.tracer.start_span(
            self._operation_name(db_name, statement),
            kind=trace.SpanKind.CLIENT,
        )
        with trace.use_span(span, end_on_exit=False):
            if span.is_recording():
                span.set_attribute(SpanAttributes.DB_SYSTEM, self.vendor)
                span.set_attribute(SpanAttributes.DB_STATEMENT, statement)
                if self.sanitize_query:
                    span.set_attribute(
                        SpanAttributes.DB_STATEMENT, _sanitize_query(statement)
                    )
                for key, value in attrs.items():
                    span.set_attribute(key, value)
            if self.enable_commenter:
                commenter_data = dict(
                    db_driver=conn.engine.driver,
                    # Driver/framework centric information.
                    db_framework=f"sqlalchemy:{__version__}",
                )

                if self.commenter_options.get("opentelemetry_values", True):
                    commenter_data.update(**_get_opentelemetry_values())

                # Filter down to just the requested attributes.
                commenter_data = {
                    k: v
                    for k, v in commenter_data.items()
                    if self.commenter_options.get(k, True)
                }

                statement = _add_sql_comment(statement, **commenter_data)

        context._otel_span = span

        return statement, params


# pylint: disable=unused-argument
def _after_cur_exec(conn, cursor, statement, params, context, executemany):
    span = getattr(context, "_otel_span", None)
    if span is None:
        return

    span.end()


def _handle_error(context):
    span = getattr(context.execution_context, "_otel_span", None)
    if span is None:
        return

    if span.is_recording():
        span.set_status(
            Status(
                StatusCode.ERROR,
                str(context.original_exception),
            )
        )
    span.end()


def _get_attributes_from_url(url):
    """Set connection tags from the url. return true if successful."""
    attrs = {}
    if url.host:
        attrs[SpanAttributes.NET_PEER_NAME] = url.host
    if url.port:
        attrs[SpanAttributes.NET_PEER_PORT] = url.port
    if url.database:
        attrs[SpanAttributes.DB_NAME] = url.database
    if url.username:
        attrs[SpanAttributes.DB_USER] = url.username
    return attrs, bool(url.host)


def _get_attributes_from_cursor(vendor, cursor, attrs):
    """Attempt to set db connection attributes by introspecting the cursor."""
    if vendor == "postgresql":
        info = getattr(getattr(cursor, "connection", None), "info", None)
        if not info:
            return attrs

        attrs[SpanAttributes.DB_NAME] = info.dbname
        is_unix_socket = info.host and info.host.startswith("/")

        if is_unix_socket:
            attrs[SpanAttributes.NET_TRANSPORT] = NetTransportValues.UNIX.value
            if info.port:
                # postgresql enforces this pattern on all socket names
                attrs[SpanAttributes.NET_PEER_NAME] = os.path.join(
                    info.host, f".s.PGSQL.{info.port}"
                )
        else:
            attrs[
                SpanAttributes.NET_TRANSPORT
            ] = NetTransportValues.IP_TCP.value
            attrs[SpanAttributes.NET_PEER_NAME] = info.host
            if info.port:
                attrs[SpanAttributes.NET_PEER_PORT] = int(info.port)
    return attrs
