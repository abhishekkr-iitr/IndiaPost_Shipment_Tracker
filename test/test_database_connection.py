
import os
from pathlib import Path

from database.db_connection import get_db_connection


try:
    connection = get_db_connection()

    try:
        actual_database = connection.execute(
            "PRAGMA database_list;"
        ).fetchone()[2]

        print("Database connection successful!")
        print("Connected database:", Path(actual_database).resolve())

        # Verify that the connection can execute a query.
        connection.execute("SELECT 1;")

        print("Database query executed successfully!")

    finally:
        connection.close()

    print("Database connection closed successfully!")

except Exception as error:
    print("Database connection failed:", error)
    raise
