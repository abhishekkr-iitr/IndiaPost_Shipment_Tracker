"""Focused tests for local application environment loading."""

import unittest
from pathlib import Path
from unittest.mock import patch

import app


class LocalApplicationEnvironmentTests(unittest.TestCase):
    def test_loads_project_env_without_overriding_os_environment(self):
        expected_path = Path(app.__file__).resolve().parent / ".env"
        with patch("app.load_dotenv", return_value=True) as mocked_load:
            result = app.load_application_environment()

        self.assertTrue(result)
        mocked_load.assert_called_once_with(
            dotenv_path=expected_path,
            override=False,
        )


if __name__ == "__main__":
    unittest.main()
