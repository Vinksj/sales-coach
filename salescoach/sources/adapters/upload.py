"""Upload: a transcript file in any format parsers.parse_any reads (and the paste box beside it)."""
from typing import Optional

from .. import base, parsers
from . import MAX_BYTES, Adapter, SourceError


def normalize_file(kind: str, data: bytes, filename: Optional[str] = None, title: Optional[str] = None,
                   trusted: bool = True) -> base.NormalizedTranscript:
    """Bytes of an exported transcript -> NormalizedTranscript. Shared with the folder adapter, and the
    reference is a digest of the content, so the same file arriving both ways is one call.
    trusted: base.from_parsed's rule. True for a file the seller uploads by hand, False for a file that
    turned up in the watched folder."""
    if len(data) > MAX_BYTES:
        raise SourceError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB; a transcript never is "
                          "(a recording goes through Upload a recording)")
    parsed = parsers.parse_any(data, filename)
    stem = (filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").strip()
    return base.from_parsed(parsed, kind, raw=parsers.decode(data), source_ref=base.content_ref("file", data),
                            title=title or parsed.title or stem or "Imported call", trusted=trusted)


class UploadAdapter(Adapter):
    kind = "upload"
    label = "Upload a transcript"
    how = ("Export the transcript from any recorder (.txt, .vtt, .srt, Otter text, Fireflies or Fathom JSON) and "
           "upload it on the Import page. Pasting text works too.")
    mode = "push"
    default_enabled = True
    default_poll_minutes = None

    def normalize(self, data: bytes, filename: Optional[str] = None, title: Optional[str] = None):
        return normalize_file(self.kind, data, filename, title)
