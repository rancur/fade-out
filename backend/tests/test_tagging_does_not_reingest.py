"""End-to-end: a tagged file must not look new to the watcher.

This is the guarantee the whole out-of-place design exists to provide. It is
asserted against the watcher's own dedupe state, not a mock of it.
"""
import os
import subprocess

from app.services import file_watcher, source_tagger


def _make_flac(path):
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-c:a", "flac", "-y", str(path)],
        check=True, capture_output=True,
    )


def test_tagged_file_is_already_known_to_the_watcher(tmp_path):
    watch = tmp_path / "audio"
    watch.mkdir()
    src = watch / "mix.flac"
    _make_flac(src)

    db = file_watcher.open_seen_files_db(str(tmp_path / "seen.db"))
    # The untagged original has been ingested already, as in production.
    db.mark_done(file_watcher.compute_file_hash(str(src)), str(src), "audio")

    temp = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T"}, None, None
    )
    source_tagger.register_and_promote(temp, str(src), "audio", seen_db=db)

    # The watcher's question, asked exactly as the watcher asks it.
    assert db.is_done(file_watcher.compute_file_hash(str(src))), (
        "the tagged file is unknown to the watcher -- it would be re-ingested "
        "and the mix re-uploaded"
    )


def test_the_staging_dir_is_invisible_to_the_watcher_scan(tmp_path):
    """The watcher lists one level; the staging dir must not yield candidates."""
    watch = tmp_path / "audio"
    watch.mkdir()
    src = watch / "mix.flac"
    _make_flac(src)
    source_tagger.write_tagged_copy(str(src), {"ARTIST": "Will See"}, None, None)

    entries = os.listdir(str(watch))
    assert source_tagger.TEMP_DIRNAME in entries
    audio_files = [
        e for e in entries
        if os.path.isfile(os.path.join(str(watch), e))
        and os.path.splitext(e)[1].lower() in file_watcher.AUDIO_EXTENSIONS
    ]
    assert audio_files == ["mix.flac"], (
        "a non-recursive listdir must not surface the staged copy as a new recording"
    )


def test_file_is_never_visible_in_the_watch_folder_before_registration(tmp_path):
    """The window property: file is never visible while its hash is unregistered.

    The end-state test above proves the final content is safe. It cannot prove the
    ORDER: rename-then-register leaves the same state and would pass. This test
    fails if the file ever becomes visible at its final path before its hash is
    registered, which is the actual guarantee -- a watcher scanning at that instant
    would ingest a published mix and re-upload it.
    """
    watch = tmp_path / "audio"
    watch.mkdir()
    src = watch / "mix.flac"
    _make_flac(src)
    final_path = str(src)

    observed = {}

    class _WindowCheckingDB:
        def __init__(self):
            self.done = set()

        def mark_done(self, file_hash, file_path, file_type):
            # At this instant, capture the hash of the file at its final path.
            # If registration happens after rename, this will be the tagged file's hash.
            # If registration happens before rename (correct), this will be the original's hash.
            if os.path.exists(file_path):
                observed["hash_at_registration"] = file_watcher.compute_file_hash(file_path)
            else:
                observed["hash_at_registration"] = None
            self.done.add(file_hash)

        def is_done(self, file_hash):
            return file_hash in self.done

        def close(self):
            pass

    db = _WindowCheckingDB()
    temp = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T"}, None, None
    )
    source_tagger.register_and_promote(temp, final_path, "audio", seen_db=db)

    assert observed.get("hash_at_registration") is not None, (
        "mark_done was never called -- the tagged file would be unknown to the watcher"
    )

    # The decisive check: at registration time, the file at final_path held the
    # ORIGINAL untagged content. After promotion, it holds the TAGGED content.
    # If the rename happened before registration, both hashes would be the same
    # (the tagged version), which would be the bug this test exists to catch.
    hash_at_registration = observed["hash_at_registration"]
    final_hash = file_watcher.compute_file_hash(final_path)
    assert hash_at_registration != final_hash, (
        "at registration time the file already contained tagged bytes -- "
        "the rename happened before registration, and the file was visible "
        "while unknown to the watcher"
    )

    # Confirm the final state is also correct: the new hash is registered.
    assert db.is_done(final_hash)
