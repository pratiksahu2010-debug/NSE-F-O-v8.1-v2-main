"""Explicit in-memory DB-API connection for unit tests only; production DB has no local fallback."""
import sqlite3
from db import DB


def make_test_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return DB(connection=connection)
