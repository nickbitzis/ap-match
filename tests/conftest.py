import pytest

from apmatch.pipeline import Config, run


@pytest.fixture(scope="session")
def results():
    """One small end to end run shared by the integration tests (about 2 seconds)."""
    return run(Config(suppliers=50, pos=2000, seed=7))
