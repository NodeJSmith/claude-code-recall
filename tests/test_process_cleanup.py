from ccrecall import process_cleanup


def test_process_start_identity_returns_none_when_proc_entry_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "ccrecall.process_cleanup.Path.read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    assert process_cleanup.process_start_identity(123) is None


def test_process_group_absent_fails_closed_when_proc_scan_is_unreadable(monkeypatch):
    monkeypatch.setattr(
        "ccrecall.process_cleanup.Path.glob",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )

    assert not process_cleanup.process_group_absent(123)


def test_process_group_absent_ignores_zombies_and_detects_live_members(monkeypatch):
    class StatPath:
        def __init__(self, contents):
            self.contents = contents

        def read_text(self, **_kwargs):
            return self.contents

    monkeypatch.setattr(
        "ccrecall.process_cleanup.Path.glob",
        lambda *_args, **_kwargs: [StatPath("1 (zombie) Z 1 99"), StatPath("2 (live) S 1 42")],
    )

    assert process_cleanup.process_group_absent(99)
    assert not process_cleanup.process_group_absent(42)
