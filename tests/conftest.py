import pytest

from rig import Rig
from ledger_rig import ledger_rig, plugin, plugin_check


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)
