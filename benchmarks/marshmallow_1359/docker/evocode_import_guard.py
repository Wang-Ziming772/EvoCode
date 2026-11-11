"""Fail the pytest session if it imports a package outside the test snapshot."""

from pathlib import Path
import platform


def pytest_configure(config):
    import marshmallow
    import pytest

    actual = Path(marshmallow.__file__).resolve()
    expected = Path("/workspace/src/marshmallow/__init__.py")
    print("EVOCODE_MARSHMALLOW_FILE={}".format(actual), flush=True)
    print("EVOCODE_PYTHON_VERSION={}".format(platform.python_version()), flush=True)
    print("EVOCODE_PYTEST_VERSION={}".format(pytest.__version__), flush=True)
    if actual != expected:
        raise RuntimeError("pytest did not import marshmallow from the mounted workspace")
