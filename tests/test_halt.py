from kalshi_bot import halt


def test_halt_file_lifecycle(tmp_path):
    p = tmp_path / "HALT"
    assert not halt.halt_active(p)
    halt.engage(p, "test")
    assert halt.halt_active(p)
    assert p.read_text().strip() == "test"
    assert halt.release(p)
    assert not halt.halt_active(p)
    assert not halt.release(p)
