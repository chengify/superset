#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from flask import current_app as app
from sqlalchemy.sql import compiler

from superset.constants import EXAMPLES_DB_UUID

if TYPE_CHECKING:
    from superset.connectors.sqla.models import Database

logging.getLogger("MARKDOWN").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


def get_or_create_db(
    database_name: str, sqlalchemy_uri: str, always_create: bool | None = True
) -> Database | None:
    """
    Look up a database by name, optionally creating it if it does not exist.

    The stored SQLAlchemy URI is updated to ``sqlalchemy_uri`` when it differs, so
    configuration changes are reflected on the existing database reference.

    :param database_name: The unique name of the database
    :param sqlalchemy_uri: The SQLAlchemy URI the database should point to
    :param always_create: Whether to create the database when it is missing
    :returns: The database, or ``None`` if it is missing and ``always_create`` is falsy
    """
    # pylint: disable=import-outside-toplevel
    from superset import db
    from superset.daos.database import DatabaseDAO

    database = DatabaseDAO.get_database_by_name(database_name)

    # databases with a fixed UUID
    uuids = {
        "examples": EXAMPLES_DB_UUID,
    }

    if not database and always_create:
        logger.info("Creating database reference for %s", database_name)
        database = DatabaseDAO.create(
            attributes={
                "database_name": database_name,
                "uuid": uuids.get(database_name),
            }
        )
        database.set_sqlalchemy_uri(sqlalchemy_uri)

    if database and database.sqlalchemy_uri_decrypted != sqlalchemy_uri:
        database.set_sqlalchemy_uri(sqlalchemy_uri)

    db.session.flush()
    return database


def _get_or_create_required_db(database_name: str, sqlalchemy_uri: str) -> Database:
    database = get_or_create_db(database_name, sqlalchemy_uri, always_create=True)
    if database is None:
        raise RuntimeError(f"Unable to get or create database {database_name!r}")
    return database


def get_example_database() -> Database:
    """Return the database reference for the examples, creating it if needed."""
    return _get_or_create_required_db("examples", app.config["SQLALCHEMY_EXAMPLES_URI"])


def get_main_database() -> Database:
    """Return the database reference for the metadata DB, creating it if needed."""
    return _get_or_create_required_db("main", app.config["SQLALCHEMY_DATABASE_URI"])


def warm_and_release_connection(instance: Any, *relationships: str) -> None:
    """
    Eagerly load the named relationships on ``instance``, then release the
    current session's DB connection back to the pool without detaching any
    object in the session.

    Prefer this over ``db.session.close()`` before slow, non-DB work (a
    long-running cursor execution, a results-backend fetch, CPU-bound
    decompress/deserialize work) that still needs attributes already
    loaded on session objects: ``close()`` detaches every object in the
    session -- including ``g.user``, not just ``instance`` -- so a later
    attribute access anywhere in the request can raise on a detached
    instance or silently open a fresh connection. Committing with
    ``expire_on_commit`` disabled instead releases the connection while
    keeping objects attached and their already-loaded attributes valid.
    """
    # pylint: disable=import-outside-toplevel
    from superset import db

    for relationship in relationships:
        getattr(instance, relationship)

    # ``db.session`` is a ``scoped_session`` proxy: it only forwards a fixed
    # allowlist of attributes to the real ``Session`` (bind, dirty, deleted,
    # new, identity_map, is_active, autoflush, no_autoflush, info).
    # ``expire_on_commit`` isn't on that list, so setting it on ``db.session``
    # directly would silently no-op -- it has to be set on the real Session
    # returned by calling the proxy.
    session = db.session()
    session.expire_on_commit = False
    try:
        session.commit()  # pylint: disable=consider-using-transaction
    finally:
        session.expire_on_commit = True


def apply_mariadb_ddl_fix() -> None:
    """
    Fix MariaDB "NO CYCLE" syntax issue - MariaDB uses "NOCYCLE" (no space).

    This fix will be included in SQLAlchemy v2.1.0.
    See: https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_1_0b1/lib/sqlalchemy/dialects/mysql/_mariadb_shim.py
    """
    original_visit_create_sequence = compiler.DDLCompiler.visit_create_sequence

    def patched_visit_create_sequence(self: Any, create: Any, **kw: Any) -> str:
        text = original_visit_create_sequence(self, create, **kw)
        dialect_name = getattr(self.dialect, "name", "") or ""
        if "mariadb" in dialect_name.lower():
            return text.replace("NO CYCLE", "NOCYCLE")
        return text

    compiler.DDLCompiler.visit_create_sequence = patched_visit_create_sequence
