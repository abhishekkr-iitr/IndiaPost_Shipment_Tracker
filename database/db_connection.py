
import os
import sqlite3
from pathlib import Path


# Determine the database directory
BASE_DIR = Path(__file__).resolve().parent

# Default database file location
DATABASE_PATH = BASE_DIR / "shipments.db"


def get_db_connection():
    """
    Create and return a connection to the SQLite database.

    By default, use the application's existing database.
    For isolated testing, INDIAPOST_DB_PATH can specify
    an alternative database file.
    """

    database_override = os.environ.get("INDIAPOST_DB_PATH")

    if database_override:
        database_path = Path(database_override).expanduser().resolve()
    else:
        database_path = DATABASE_PATH

    connection = sqlite3.connect(database_path)

    # Enable foreign key constraints
    connection.execute("PRAGMA foreign_keys = ON")

    return connection
