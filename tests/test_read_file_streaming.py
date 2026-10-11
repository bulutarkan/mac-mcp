"""#168: read_file/read_multiple_files hold only what they return, not the whole file."""
from __future__ import annotations

import os
import shutil
import tempfile
import tracemalloc
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_files
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
from mcp_server.policy_scope import ResourceScope
from mcp_server.security import truncate

SAMPLES = {
    "empty": b"",
    "no_break": b"abc",
    "lf": b"a\nb\nc\n",
    "crlf": b"a\r\nb\r\nc",
    "cr": b"a\rb\rc\r",
    "unicode_breaks": "x y z\x0bw\x0cv\x1cu\x85t\n".encode(),
    "bad_utf8": b"ok\n\xff\xfe broken \xc3\n tail \xe2\x82",
    "multibyte": ("é" * 9 + "\n" + "ğ" * 5).encode(),
    "blank_lines": b"a\n\n\n",
    "many": b"".join(b"row %d\n" % i for i in range(60)),
}
WINDOWS = [(0, None), (0, 0), (1, None), (2, 3), (0, 1), (50, 20), (10_000, 5), (3, -1), (0, -2), (5, -2)]


def reference_read(text: str, offset: int, length, limit: int) -> dict:
    """The pre-#168 algorithm: whole text in memory, then slice and truncate."""
    lines = None
    if offset or length is not None:
        lines = text.splitlines(keepends=True)
        text = "".join(lines[offset: offset + length if length else None])
    bounded, truncated = truncate(text, limit)
    out = {"content": bounded, "truncated": truncated}
    if truncated or lines is not None:
        total = len(lines) if lines is not None else len(text.splitlines())
        if truncated:
            nxt = offset + text[: len(bounded) - len(tools_files._TRUNCATION_SUFFIX)].count("\n")
        else:
            nxt = offset + len(text.splitlines())
        out.update(total_lines=total, next_offset=nxt if nxt < total else None)
    return out


class StreamingReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mac-mcp-read-"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)
        for name, data in SAMPLES.items():
            (self.ws / name).write_bytes(data)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @contextmanager
    def scoped(self):
        scope = ResourceScope(path_roots=(str(self.ws),), tool_families=("files",), access_mode="workspace_write")
        token = set_policy_context(PolicyContext(profile="developer", actor="agent:t", agent_id="agt_t", scope=scope))
        try:
            yield
        finally:
            reset_policy_context(token)

    def check_matrix(self, decode) -> None:
        # Tiny chunks and a tiny limit put chunk and truncation edges inside every sample.
        with patch.object(tools_files, "_READ_CHUNK_CHARS", 7), patch.object(tools_files, "MAX_READ_CHARS", 23):
            for name, data in SAMPLES.items():
                text = decode(data)
                for offset, length in WINDOWS:
                    with self.subTest(file=name, offset=offset, length=length):
                        got = tools_files.read_file(None, str(self.ws / name), offset, length)
                        want = reference_read(text, offset, length, 23)
                        self.assertEqual(want, {k: got[k] for k in want})

    def test_plain_reads_match_the_whole_file_algorithm(self) -> None:
        # Path.read_text: universal newlines.
        self.check_matrix(lambda data: data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n"))

    def test_scoped_reads_match_the_whole_file_algorithm(self) -> None:
        # scoped_read_text: bytes decoded as-is, \r\n kept.
        with self.scoped():
            self.check_matrix(lambda data: data.decode("utf-8", errors="replace"))

    def test_read_multiple_files_reports_the_same_counts_and_cuts(self) -> None:
        paths = [str(self.ws / name) for name in SAMPLES]
        with patch.object(tools_files, "_READ_CHUNK_CHARS", 5), patch.object(tools_files, "READ_MULTIPLE_PER_FILE_CHARS", 9):
            files = tools_files.read_multiple_files(None, paths, 1_000)["files"]
        for name, record in zip(SAMPLES, files):
            text = SAMPLES[name].decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            with self.subTest(file=name):
                self.assertEqual(len(text), record["chars"])
                self.assertEqual(truncate(text, 9)[0], record["content"])

    def test_peak_memory_follows_the_slice_not_the_file(self) -> None:
        big = self.ws / "big.log"
        line = b"2026-10-11 INFO handled request " + b"x" * 90 + b"\n"
        with open(big, "wb") as handle:
            handle.writelines(line * 1000 for _ in range(200))  # ~24 MB
        for call in (lambda: tools_files.read_file(None, str(big), 10, 2),
                     lambda: tools_files.read_file(None, str(big)),
                     lambda: tools_files.read_multiple_files(None, [str(big)])):
            tracemalloc.start()
            try:
                call()
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 12_000_000)  # the old whole-file read peaked at well over 2x the file size

    def test_a_file_without_line_breaks_is_not_held_whole(self) -> None:
        huge = self.ws / "one_line"
        huge.write_bytes(b"y" * 8_000_000)
        tracemalloc.start()
        try:
            result = tools_files.read_file(None, str(huge), 0, 1)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(result["truncated"])
        self.assertEqual(1, result["total_lines"])
        self.assertLess(peak, 6_000_000)


if __name__ == "__main__":
    unittest.main()
