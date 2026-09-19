"""Watched folder: every transcript file saved into data/inbox/drop/ is imported once.

A file that was imported moves to drop/processed/, so the folder is "what has not been read yet".
A file the coach could NOT read as a transcript is left exactly where it is: it is somebody's file,
and a folder the user pointed at may hold things that were never meant for the coach. It is
remembered (by content hash, under the state key sources:folder:skipped) so it is not parsed again
every minute, and the reason is on record there and in the source's last error.

While a file is being parsed it carries the suffix `.processing`. If the process dies mid-parse (a
crash, a kill, a parser that never returns), the next poll finds the leftover, gives it its name
back and remembers it as skipped instead of walking into the same wall on every start.

Where the folder may be (refusal()): never the home folder, the filesystem root, the top level of
Desktop / Documents / Downloads, or a folder that contains the coach's own install, data or settings.
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from ... import config
from . import MAX_BYTES, Adapter, MeetingRef, SourceError
from .upload import normalize_file

SETTLE_S = 3.0                # a file still being written is left for the next round
SUFFIXES = {".txt", ".vtt", ".srt", ".json", ".text"}       # transcript exports; not "" and not .md (review 3)
PROCESSING = ".processing"
STALE_PROCESSING_S = 120.0    # a .processing file older than this belongs to a run that died
SKIPPED_KEY = "skipped"       # state key sources:folder:skipped
MAX_SKIPPED = 500
_BROAD = ("Desktop", "Documents", "Downloads")


def refusal(raw) -> Optional[str]:
    """Why this folder cannot be the watched folder, or None. The folder adapter reads, moves and
    sends to a model whatever transcript-looking file it finds, so it must be a folder FOR that."""
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        return "Give the folder's full path, starting with / or ~."
    try:
        real = path.resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return "That folder path cannot be used."
    if real == Path(real.anchor):
        return "The whole disk cannot be the watched folder. Choose a folder that only holds call transcripts."
    if real == home:
        return "Your home folder cannot be the watched folder. Choose a folder that only holds call transcripts."
    for name in _BROAD:
        if real == (home / name).resolve():
            return (f"{name} itself cannot be the watched folder: the coach would read and move every text file in "
                    f"it. Make a folder inside it (for example {name}/Call transcripts) and use that.")
    for label, own in (("the coach's own program folder", config.ROOT), ("the coach's data folder", config.DATA_DIR),
                       ("the coach's settings folder", config.user_dir())):
        try:
            own = Path(own).resolve()
        except (OSError, RuntimeError):
            continue
        if real == own or real in own.parents:
            return f"That folder contains {label}, so it cannot be the watched folder."
    return None


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FolderAdapter(Adapter):
    kind = "folder"
    label = "Watched folder"
    how = ("Save or drag transcript files into the drop folder (data/inbox/drop). Each one is imported once and "
           "moved to processed/. A file that is not a transcript is left where it is.")
    default_enabled = True
    default_poll_minutes = 1

    def __init__(self, options: Optional[dict] = None):
        super().__init__(options)
        self._skipped: Optional[dict] = None           # {sha: {name, size, mtime_ns, error, at}}

    # ---- where ------------------------------------------------------------------------------

    def drop_dir(self) -> Path:
        custom = self.options.get("path")
        return Path(os.path.expanduser(str(custom))) if custom else Path(config.DATA_DIR) / "inbox" / "drop"

    def refused(self) -> Optional[str]:
        return refusal(self.options.get("path"))

    def configured(self) -> bool:
        return True

    def _path(self, ext_id: str) -> Path:
        path = self.drop_dir() / ext_id
        if Path(ext_id).name != ext_id or not path.is_file() or path.is_symlink():
            raise SourceError(f"{ext_id}: not a file in the drop folder")
        return path

    # ---- what was set aside --------------------------------------------------------------------

    def skipped(self) -> dict:
        if self._skipped is None:
            try:
                found = json.loads(self.state_get(SKIPPED_KEY) or "{}")
            except ValueError:
                found = {}
            self._skipped = found if isinstance(found, dict) else {}
        return self._skipped

    def _remember(self, path: Path, error: str, sha: Optional[str] = None) -> None:
        try:
            stat = path.stat()
            # A file too large to be a transcript is not read just to be told apart: its name, size and
            # time are its identity.
            sha = sha or (_sha(path) if stat.st_size <= MAX_BYTES else
                          f"big:{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            return
        skipped = self.skipped()
        skipped[sha] = {"name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                        "error": str(error).strip()[:300], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        for old in sorted(skipped, key=lambda k: skipped[k].get("at") or "")[:-MAX_SKIPPED]:
            del skipped[old]
        self.state_set(SKIPPED_KEY, json.dumps(skipped, sort_keys=True))

    def _is_skipped(self, path: Path, stat) -> bool:
        """Cheap first (same name, size and mtime as a remembered file), then by content, so a renamed
        copy of a file that was set aside is not parsed again either."""
        skipped = self.skipped()
        if not skipped:
            return False
        if any(e.get("name") == path.name and e.get("size") == stat.st_size and e.get("mtime_ns") == stat.st_mtime_ns
               for e in skipped.values()):
            return True
        if stat.st_size > MAX_BYTES or not any(e.get("size") == stat.st_size for e in skipped.values()):
            return False
        try:
            return _sha(path) in skipped
        except OSError:
            return False

    # ---- list / fetch ----------------------------------------------------------------------------

    def _recover(self, drop: Path) -> None:
        """A `.processing` file nobody is processing: the run that renamed it died. Give it its name back
        and set it aside; whatever stopped that run would stop this one."""
        for path in sorted(drop.glob(f"*{PROCESSING}")):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                if time.time() - max(stat.st_mtime, stat.st_ctime) < STALE_PROCESSING_S:
                    continue                           # another poll (the CLI beside the server) may be on it
                original = path.with_name(path.name[:-len(PROCESSING)])
                if original.exists():
                    original = path.with_name(f"{original.stem}.{time.strftime('%Y%m%d-%H%M%S')}{original.suffix}")
                os.replace(path, original)
                self._remember(original, "the last attempt to read this file did not finish, so it was set aside")
            except OSError:
                continue

    def list_recent(self, since=None) -> list:
        problem = self.refused()
        if problem:
            raise SourceError(problem)
        drop = self.drop_dir()
        drop.mkdir(parents=True, exist_ok=True)
        self._skipped = None                            # read the state afresh for this poll
        self._recover(drop)
        refs, cutoff = [], time.time() - SETTLE_S
        for path in sorted(drop.iterdir(), key=lambda p: p.name):
            if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in SUFFIXES:
                continue
            stat = path.stat()
            if stat.st_mtime > cutoff or self._is_skipped(path, stat):
                continue
            refs.append(MeetingRef(ext_id=path.name, source_ref="", title=path.stem))
        return refs

    def fetch(self, ext_id: str):
        if self.refused():
            raise SourceError(self.refused())
        path = self._path(ext_id)
        if path.stat().st_size > MAX_BYTES:
            raise SourceError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
        working = path.with_name(path.name + PROCESSING)
        os.replace(path, working)                       # from here a dead run leaves a trace _recover() finds
        return normalize_file(self.kind, working.read_bytes(), path.name, trusted=False)

    # ---- after ----------------------------------------------------------------------------------------

    def _current(self, ext_id: str) -> Optional[Path]:
        """The file as it is called right now: mid-import it carries the .processing suffix."""
        for path in (self.drop_dir() / (ext_id + PROCESSING), self.drop_dir() / ext_id):
            if path.is_file() and not path.is_symlink():
                return path
        return None

    def _restore(self, ext_id: str) -> Optional[Path]:
        working, original = self.drop_dir() / (ext_id + PROCESSING), self.drop_dir() / ext_id
        if working.is_file() and not original.exists():
            os.replace(working, original)
        return original if original.is_file() else None

    def after_import(self, ext_id: str, result) -> None:
        src = self._current(ext_id)
        if src is None:
            return
        dest_dir = self.drop_dir() / "processed"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ext_id
        if dest.exists():
            dest = dest_dir / f"{Path(ext_id).stem}.{time.strftime('%Y%m%d-%H%M%S')}{Path(ext_id).suffix}"
        os.replace(src, dest)

    def after_failure(self, ext_id: str, error: str, exc: Optional[BaseException] = None) -> None:
        """Nothing is moved. A file that is not a transcript (or is too large) is remembered so it is not
        read again; anything else (a locked store, a profile that is not set up yet) is simply tried again."""
        path = self._restore(ext_id)
        if path is not None and (exc is None or isinstance(exc, (ValueError, SourceError))):
            self._remember(path, error)
