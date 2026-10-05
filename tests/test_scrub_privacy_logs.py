import gzip
import json
import os
from pathlib import Path

import pytest

from ops import scrub_privacy_logs as scrub


def test_scan_and_scrub_plain_and_gzip(tmp_path):
    needle = "private.example.test"
    needles_file = tmp_path / "needles.json"
    needles_file.write_text(json.dumps([needle]))
    plain = tmp_path / "access.log"
    plain.write_text("keep one\nremove %s\nkeep two\n" % needle)
    compressed = tmp_path / "access.log.1.gz"
    with gzip.open(compressed, "wt") as stream:
        stream.write("remove https://%s/path\nkeep gzip\n" % needle)

    needles = scrub.load_needles(needles_file)
    dry = [scrub.scrub_file(path, needles, False)
            for path in scrub.iter_files([tmp_path], exclude=(needles_file,))]
    assert sum(item["removed_lines"] for item in dry) == 2
    assert needle in plain.read_text()

    applied = [scrub.scrub_file(path, needles, True)
               for path in scrub.iter_files([tmp_path], exclude=(needles_file,))]
    assert sum(item["removed_lines"] for item in applied) == 2
    assert plain.read_text() == "keep one\nkeep two\n"
    with gzip.open(compressed, "rt") as stream:
        assert stream.read() == "keep gzip\n"

    rescanned = [scrub.scrub_file(path, needles, False)
                 for path in scrub.iter_files([tmp_path], exclude=(needles_file,))]
    assert sum(item["removed_lines"] for item in rescanned) == 0


def test_refuses_hard_links(tmp_path):
    original = tmp_path / "access.log"
    linked = tmp_path / "access.log.1"
    original.write_text("private.example.test\n")
    linked.hardlink_to(original)
    with pytest.raises(ValueError, match="hard-linked"):
        scrub.scrub_file(original, (b"private.example.test",), True)


def test_replace_failure_keeps_original_log(tmp_path, monkeypatch):
    path = tmp_path / "access.log"
    original = b"keep\nprivate.example.test\n"
    path.write_bytes(original)

    def fail_replace(source, destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        scrub.scrub_file(path, (b"private.example.test",), True)
    assert path.read_bytes() == original


def test_matching_is_ascii_case_insensitive(tmp_path):
    path = tmp_path / "access.log"
    path.write_text("https://PRIVATE.EXAMPLE.TEST/path\nkeep\n")
    result = scrub.scrub_file(path, (b"private.example.test",), True)
    assert result["removed_lines"] == 1
    assert path.read_text() == "keep\n"


def test_matching_uses_unicode_casefold(tmp_path):
    path = tmp_path / "access.log"
    path.write_text("https://Ä.example/path\nkeep\n")
    result = scrub.scrub_file(path, ("ä.example".encode(),), True)
    assert result["removed_lines"] == 1
    assert path.read_text() == "keep\n"


def test_overwrite_failure_keeps_clean_path_and_retry_marker(tmp_path, monkeypatch):
    path = tmp_path / "access.log"
    original = b"keep\nprivate.example.test\n"
    path.write_bytes(original)

    def fail_overwrite(fd, size):
        os.close(fd)
        raise OSError("synthetic overwrite failure")

    monkeypatch.setattr(scrub, "_overwrite_fd", fail_overwrite)
    with pytest.raises(OSError, match="synthetic overwrite failure"):
        scrub.scrub_file(path, (b"private.example.test",), True)
    assert path.read_bytes() == b"keep\n"
    markers = list(tmp_path.glob(".*.privacy-original-*"))
    assert len(markers) == 1


def test_recovers_interrupted_original_and_expands_idn_needles(tmp_path):
    interrupted = tmp_path / ".access.log.privacy-original-deadbeef"
    interrupted.write_text("sensitive old log\n")
    recovered = scrub.recover_interrupted_scrubs([tmp_path])
    assert recovered == [str(interrupted)]
    assert (tmp_path / "access.log").read_text() == "sensitive old log\n"

    needles_file = tmp_path / "needles.json"
    needles_file.write_text(json.dumps(["faß.de"]))
    needles = scrub.load_needles(needles_file)
    assert "faß.de".encode() in needles
    assert b"xn--fa-hia.de" in needles


def test_single_file_root_recovers_interrupted_original(tmp_path):
    target = tmp_path / "single.log"
    interrupted = tmp_path / ".single.log.privacy-original-deadbeef"
    interrupted.write_text("old sensitive log\n")
    recovered = scrub.recover_interrupted_scrubs([target])
    assert recovered == [str(interrupted)]
    assert target.read_text() == "old sensitive log\n"