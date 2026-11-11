"""Shared test setup.

The repository ships one animation pack, the Quaternius one (the supported source). The tests also
need packs for the other sources the engine can convert (a Mixamo pack, a Mesh2Motion pack): they are
example profiles in `brain/tests/data/profiles/`, laid out the way a user's would be and loaded the way
a user's are, as profiles of the install, never as part of what is shipped.
"""

from __future__ import annotations

import pytest

from blender import pack_profiles
from brain.tests.example_packs import example_profiles


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_profiles: the test reads the profiles that are really installed (shipped and the user's)",
    )


@pytest.fixture(autouse=True)
def _example_profiles(request: pytest.FixtureRequest):
    if request.node.get_closest_marker("real_profiles"):
        yield
        return
    with pack_profiles.use_profiles(example_profiles()):
        yield
