#!/usr/bin/env python3
"""Remove log lines containing privacy-erasure needles.

Plain and gzip logs are rewritten in their original format. In apply mode the
old inode is overwritten before replacement so deleted lines do not remain in
the unlinked file blocks. Run only while writers and log synchronizers are
stopped.
"""
import argparse
import fnmatch
import gzip
import idna
import json
import os
from pathlib import Path
import stat
import tempfile


DEFAULT_PATTERNS = ("*.log", "*.log.*", "*.gz", "*.html", "*.json", "*.txt")


def load_needles(path: Path) -> tuple[bytes, ...]:
    values = json.loads(path.read_text())
    if not isinstance(values, list):
        raise ValueError("needles file must contain a JSON list")
    expanded = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        expanded.add(value)
        try:
            expanded.add(idna.encode(value, uts46=True).decode("ascii"))
        except idna.IDNAError:
            pass
        try:
            expanded.add(idna.decode(value.encode("ascii")))
        except (UnicodeError, idna.IDNAError):
            pass
    needles = tuple(sorted({value.encode("utf-8").lower()
                            for value in expanded}, key=len, reverse=True))
    if not needles:
        raise ValueError("needles file is empty")
    return needles


def iter_files(roots, patterns=DEFAULT_PATTERNS, exclude=()):
    seen = set()
    excluded = {Path(path).resolve() for path in exclude}
    for root in roots:
        root = Path(root)
        candidates = (root,) if root.is_file() else root.rglob("*")
        for path in candidates:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if path.resolve() in excluded:
                continue
            if path in seen or not stat.S_ISREG(info.st_mode):
                continue
            if not any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns):
                continue
            seen.add(path)
            yield path


def recover_interrupted_scrubs(roots) -> list[str]:
    recovered = []
    for root in roots:
        root = Path(root)
        if root.is_dir():
            originals = root.rglob(".*.privacy-original-*")
        else:
            originals = root.parent.glob(
                ".%s.privacy-original-*" % root.name)
        for original in originals:
            if not original.is_file():
                continue
            marker = ".privacy-original-"
            prefix = original.name.split(marker, 1)[0]
            target = original.with_name(prefix.lstrip("."))
            if target.exists():
                _overwrite_fd(os.open(original, os.O_RDWR | os.O_NOFOLLOW),
                              original.stat().st_size)
                original.unlink()
            else:
                os.rename(original, target)
            recovered.append(str(original))
    return recovered


def _reader(path: Path):
    return gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")


def _writer(stream, gzip_output: bool):
    if gzip_output:
        return gzip.GzipFile(fileobj=stream, mode="wb", mtime=0)
    return stream


def _overwrite_fd(fd: int, size: int) -> None:
    block = b"\0" * (1024 * 1024)
    with os.fdopen(fd, "r+b", buffering=0) as stream:
        stream.seek(0)
        remaining = size
        while remaining:
            chunk = block[:min(len(block), remaining)]
            stream.write(chunk)
            remaining -= len(chunk)
        stream.flush()
        os.fsync(stream.fileno())


def _line_occurrences(line: bytes, needles: tuple[bytes, ...]) -> int:
    folded_bytes = line.lower()
    folded_text = None
    total = 0
    for needle in needles:
        if any(byte >= 0x80 for byte in needle):
            if folded_text is None:
                folded_text = line.decode("utf-8", "ignore").casefold()
            total += folded_text.count(
                needle.decode("utf-8", "ignore").casefold())
        else:
            total += folded_bytes.count(needle)
    return total


def scrub_file(path: Path, needles: tuple[bytes, ...], apply: bool) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("refusing non-regular or hard-linked file: %s" % path)
    removed_lines = occurrences = kept_lines = 0
    temp_name = None
    original_name = None
    try:
        if apply:
            temp = tempfile.NamedTemporaryFile(
                mode="w+b", prefix=".%s.privacy-" % path.name,
                dir=path.parent, delete=False)
            temp_name = Path(temp.name)
            output = _writer(temp, path.suffix == ".gz")
        else:
            temp = output = None
        with _reader(path) as source:
            for line in source:
                count = _line_occurrences(line, needles)
                if count:
                    removed_lines += 1
                    occurrences += count
                    continue
                kept_lines += 1
                if output is not None:
                    output.write(line)
        if output is not None and output is not temp:
            output.close()
        if temp is not None:
            temp.flush()
            os.fsync(temp.fileno())
            os.fchmod(temp.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(temp.fileno(), info.st_uid, info.st_gid)
            temp.close()
        if apply and removed_lines:
            fd, raw_name = tempfile.mkstemp(
                prefix=".%s.privacy-original-" % path.name, dir=path.parent)
            os.close(fd)
            original_name = Path(raw_name)
            original_name.unlink()
            current_info = path.lstat()
            if (current_info.st_ino != info.st_ino
                    or current_info.st_dev != info.st_dev
                    or current_info.st_nlink != 1):
                raise ValueError("log file changed while scrubbing: %s" % path)
            os.rename(path, original_name)
            try:
                os.replace(temp_name, path)
            except BaseException:
                os.rename(original_name, path)
                original_name = None
                raise
            temp_name = None
            old_fd = os.open(original_name, os.O_RDWR | os.O_NOFOLLOW)
            try:
                _overwrite_fd(old_fd, os.fstat(old_fd).st_size)
            except BaseException:
                raise
            original_name.unlink()
            original_name = None
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return {
            "path": str(path),
            "changed": bool(removed_lines),
            "removed_lines": removed_lines,
            "occurrences": occurrences,
            "kept_lines": kept_lines,
        }
    finally:
        if temp_name is not None:
            temp_name.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--needles-file", type=Path, required=True)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument("--pattern", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    recovered = recover_interrupted_scrubs(args.root)
    needles = load_needles(args.needles_file)
    patterns = tuple(args.pattern) or DEFAULT_PATTERNS
    results = [
        scrub_file(path, needles, args.apply)
        for path in iter_files(args.root, patterns, (args.needles_file,))
    ]
    changed = [result for result in results if result["changed"]]
    print(json.dumps({
        "mode": "apply" if args.apply else "scan",
        "files_scanned": len(results),
        "files_changed": len(changed),
        "lines_removed": sum(result["removed_lines"] for result in changed),
        "occurrences_removed": sum(result["occurrences"] for result in changed),
        "changed_paths": [result["path"] for result in changed],
        "recovered_paths": recovered,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())