"""Pytest configuration for Arbiter tests."""



def pytest_configure(config):
    """Set asyncio_mode to auto for all async tests."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
