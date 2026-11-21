import pytest

from rig import Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)
