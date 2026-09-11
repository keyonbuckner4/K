import pytest

from kalshi_bot.errors import ConfigError
from kalshi_bot.lock import InstanceLock


def test_second_instance_is_refused_until_release(tmp_path):
    path = tmp_path / "bot.demo.lock"
    first = InstanceLock(path).acquire()
    with pytest.raises(ConfigError, match="already active"):
        InstanceLock(path).acquire()
    first.release()
    second = InstanceLock(path).acquire()  # released lock can be taken again
    second.release()
    assert path.exists()
