"""The tagger depends on hashing and seen-file registration being public.

These are the two pieces of file_watcher that source_tagger must reuse exactly.
If either changes shape, tagging silently stops protecting against re-ingest,
so the contract is pinned here rather than left implicit.
"""
from app.services import file_watcher


def test_compute_file_hash_is_public_and_stable(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"x" * 2048)
    first = file_watcher.compute_file_hash(str(p))
    assert first == file_watcher.compute_file_hash(str(p))
    assert len(first) == 32


def test_compute_file_hash_changes_when_the_head_changes(tmp_path):
    """The premise of the whole design: editing the head changes the hash."""
    p = tmp_path / "a.bin"
    p.write_bytes(b"A" + b"x" * 2048)
    before = file_watcher.compute_file_hash(str(p))
    p.write_bytes(b"B" + b"x" * 2048)
    assert file_watcher.compute_file_hash(str(p)) != before


def test_open_seen_files_db_roundtrips_done_state(tmp_path):
    db = file_watcher.open_seen_files_db(str(tmp_path / "seen.db"))
    assert db.is_done("deadbeef") is False
    db.mark_done("deadbeef", "/watch/audio/x.flac", "audio")
    assert db.is_done("deadbeef") is True
