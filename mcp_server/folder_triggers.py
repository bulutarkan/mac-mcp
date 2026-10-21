"""Tasks that run when a file arrives in a folder (#193).

One thread watches every such task's folder with kqueue (no extra dependency, no polling while
nothing happens) and looks again once a minute in case an event was missed. For each new file:

- it waits until the file is written: its size and time unchanged for SETTLE_S;
- it ignores hidden files, folders, links and downloads still in progress (.crdownload, .part...);
- it runs the task once per file: a file is remembered by name, size, time and file number (so a
  new file put in its place with the same size and time is still new), and the list is
  kept in the task database, so a restart neither repeats a file nor misses one that arrived
  while Mac MCP was off;
- files the task's own run writes into the folder do not start it again. A file that appears while a
  run is going is held until the run ends, then: one macOS marks as downloaded (com.apple.quarantine:
  browsers, Mail, AirDrop, Messages) arrived from outside and starts the task; one written on this
  Mac without that mark is taken as the run's own output. Nothing that arrives during a run is dropped
  unseen;
- at most MAX_RUNS_PER_HOUR runs an hour; files beyond that are recorded as skipped;
- with batch on (#58), files that arrive together are handled in one run: once every new file is written
  and none came for BATCH_QUIET_S, one run gets them all (at most MAX_BATCH_FILES, oldest first; the rest
  make the next batch). An AI task reads them as a list in its prompt; a script or recipe gets the path
  of a JSON list of them ({file}, $MAC_MCP_TRIGGER_FILES), never the paths pasted into a command.

The files already in the folder when the task is set up do not start it.
"""
from __future__ import annotations

import copy
import ctypes
import ctypes.util
import logging
import os
import select
import shlex
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import schedule_rules
from . import scheduled_tasks as store

log = logging.getLogger("mac_mcp.folder_triggers")

SETTLE_S = 3.0
PENDING_POLL_S = 1.0
SAFETY_SCAN_S = 60.0
QUIET_AFTER_RUN_S = 5.0
MAX_RUNS_PER_HOUR = 30
BATCH_QUIET_S = 10.0
MAX_BATCH_FILES = 50
IN_PROGRESS_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp", ".icloud", ".opdownload")
REASON_CAPPED = f"More than {MAX_RUNS_PER_HOUR} files arrived within an hour, so this one was not run"


def listing(schedule: Dict[str, Any], scanned: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, List[float]]]:
    """{name: [size, mtime, inode]} of the folder's files that can start the task; None if the folder is gone.

    scanned, when given, keeps each folder's listing for one pass, so tasks that watch the
    same folder share one read of it (#255).
    """
    folder = schedule["folder"]
    if scanned is None:
        files = _scan(folder)
    else:
        if folder not in scanned:
            scanned[folder] = _scan(folder)
        files = scanned[folder]
    if files is None:
        return None
    return {name: list(value) for name, value in files.items() if schedule_rules.matches(schedule, name)}


def _scan(folder: str) -> Optional[Dict[str, List[float]]]:
    files: Dict[str, List[float]] = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith(".") or name.lower().endswith(IN_PROGRESS_SUFFIXES):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                files[name] = [info.st_size, round(info.st_mtime, 3), info.st_ino]
    except OSError:
        return None
    return files


def _same_file(remembered: Optional[List[float]], current: List[float]) -> bool:
    """Whether a remembered file is the one there now. Entries saved before the file number was
    kept ([size, mtime]) are compared on those two, so an upgrade does not make every file new (#56)."""
    if not remembered:
        return False
    return list(remembered) == list(current[:len(remembered)])


def newest_file(schedule: Dict[str, Any]) -> Optional[str]:
    """The most recent file that matches, for Run now on a task that waits for files."""
    files = listing(schedule) or {}
    if not files:
        return None
    name = max(files, key=lambda item: files[item][1])
    return os.path.join(schedule["folder"], name)


def with_file(definition: Dict[str, Any], path: str) -> Dict[str, Any]:
    """The definition for one run: {file} filled in (shell-quoted where a shell or arguments read it)."""
    out = copy.deepcopy(definition)
    quoted = shlex.quote(path)
    if out.get("kind") == "ai":
        prompt = str(out.get("prompt") or "")
        out["prompt"] = (prompt.replace(store.FILE_TOKEN, path) if store.FILE_TOKEN in prompt
                         else f"{prompt}\n\nThe file that started this run: {path}")
    for holder in (out, out.get("data_step") or {}):
        for key in ("args", "command"):
            if holder.get(key):
                holder[key] = str(holder[key]).replace(store.FILE_TOKEN, quoted)
    if isinstance(out.get("values"), dict):  # a recipe's values take the path as it is (#257)
        out["values"] = {key: value.replace(store.FILE_TOKEN, path) if isinstance(value, str) else value
                         for key, value in out["values"].items()}
    out["trigger_file"] = path
    return out


def with_files(definition: Dict[str, Any], paths: List[str]) -> Dict[str, Any]:
    """The definition for one run over several files (#58). The AI prompt lists them; a script's or a recipe's
    {file} becomes the path of a JSON list the runner writes for the run (trigger_files), so no path is ever
    pasted into a command line."""
    out = copy.deepcopy(definition)
    listed = "\n".join(f"- {path}" for path in paths)
    if out.get("kind") == "ai":
        prompt = str(out.get("prompt") or "")
        out["prompt"] = (prompt.replace(store.FILE_TOKEN, "\n" + listed) if store.FILE_TOKEN in prompt
                         else f"{prompt}\n\nThe {len(paths)} files that started this run:\n{listed}")
    out["trigger_files"] = list(paths)
    out["trigger_file"] = paths[0]
    return out


_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                           ctypes.c_int]
_libc.getxattr.restype = ctypes.c_ssize_t
_XATTR_NOFOLLOW = 0x0001


def arrived_from_outside(path: str) -> bool:
    """True when macOS marked the file as downloaded (com.apple.quarantine)."""
    try:
        return _libc.getxattr(os.fsencode(path), b"com.apple.quarantine", None, 0, 0, _XATTR_NOFOLLOW) >= 0
    except (OSError, ValueError):
        return False


class FolderTriggers:
    """Decides which files start which task; `start(task_id, definition, path)` returns started/busy."""

    def __init__(self, start: Callable[[str, Dict[str, Any], str], str], *, clock: Callable[[], float] = time.time,
                 start_batch: Optional[Callable[[str, Dict[str, Any], List[str]], str]] = None):
        self._start = start
        self._start_batch = start_batch
        self.clock = clock
        self._states: Dict[str, Dict[str, Any]] = {}
        self._wake_r, self._wake_w = -1, -1
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dirty = True
        self.scans = 0

    # Decisions (testable with a fake clock)

    def check(self, now: Optional[float] = None) -> bool:
        """One look at every watched folder. True while a file is still settling."""
        now = self.clock() if now is None else now
        self.scans += 1
        tasks = store.folder_tasks()
        live = {task["id"] for task in tasks}
        for task_id in list(self._states):
            if task_id not in live:
                del self._states[task_id]
        settling = False
        scanned: Dict[str, Any] = {}  # one read of each folder, however many tasks watch it
        for task in tasks:
            try:
                settling = self._check_task(task["id"], task["definition"], now, scanned) or settling
            except Exception:
                log.exception("folder trigger for %s failed", task["id"])
        return settling

    def _state(self, task_id: str, schedule: Dict[str, Any], files: Dict[str, List[float]], now: float) -> Dict[str, Any]:
        state = self._states.get(task_id) or store.watch_state(task_id)
        if not state or state.get("folder") != schedule["folder"] or state.get("match") != schedule.get("match"):
            # A new task, or a different folder: what is there now is the starting point.
            state = {"folder": schedule["folder"], "match": schedule.get("match"), "seen": dict(files)}
            store.save_watch_state(task_id, state, now)
        state.setdefault("pending", {})
        state.setdefault("held", {})
        self._states[task_id] = state
        return state

    def _check_task(self, task_id: str, definition: Dict[str, Any], now: float,
                    scanned: Optional[Dict[str, Any]] = None) -> bool:
        schedule = definition["schedule"]
        files = listing(schedule, scanned)
        if files is None:
            return False  # the folder is gone or unreadable; the task waits until it is back
        state = self._state(task_id, schedule, files, now)
        seen: Dict[str, List[float]] = state["seen"]
        pending: Dict[str, List[float]] = state["pending"]
        changed = False
        for name in [name for name in seen if name not in files]:
            del seen[name]
            changed = True
        held: Dict[str, List[float]] = state["held"]
        for name in [name for name in pending if name not in files]:
            del pending[name]
        for name in [name for name in held if name not in files]:
            del held[name]
            changed = True
        window = store.last_run_window(task_id)
        running = store.is_running(task_id)
        ready: List[str] = []
        for name, sig in sorted(files.items(), key=lambda item: item[1][1]):
            mtime = sig[1]
            if _same_file(seen.get(name), sig):
                if len(seen[name]) < len(sig):
                    seen[name] = list(sig)  # an entry from before #56 gets its file number
                    changed = True
                continue
            if name in held or (name not in pending and self._own_output(running, window, mtime, now)):
                if running:
                    held[name] = list(sig)  # decided once the run is over
                    changed = True
                    continue
                held.pop(name, None)
                changed = True
                if not arrived_from_outside(os.path.join(schedule["folder"], name)):
                    seen[name] = list(sig)  # written on this Mac around the run: its own output, no loop
                    continue
            entry = pending.get(name)
            if entry is None or entry[:-1] != list(sig):
                pending[name] = [*sig, now]  # new, or still being written
            elif now - entry[-1] >= SETTLE_S:
                ready.append(name)
        if schedule.get("batch") and self._start_batch is not None:
            changed = self._batch(task_id, definition, schedule, state, ready, running, now) or changed
            if changed:
                store.save_watch_state(task_id, {key: state[key] for key in ("folder", "match", "seen", "held")}, now)
            return bool(pending)
        capped: List[str] = []
        for name in ready:
            if running:
                break  # one run at a time; the rest wait their turn
            if store.file_runs_since(task_id, now - 3600) >= MAX_RUNS_PER_HOUR:
                capped.append(name)
                continue
            path = os.path.join(schedule["folder"], name)
            if self._start(task_id, definition, path) != "started":
                break
            seen[name] = pending.pop(name)[:-1]
            changed = True
            running = True
        if capped:
            store.record_skip(task_id, scheduled_for=now, reason=REASON_CAPPED, count=len(capped), now=now,
                              trigger="file", trigger_file=os.path.join(schedule["folder"], capped[0]))
            for name in capped:
                seen[name] = pending.pop(name)[:-1]
            changed = True
        if changed:
            store.save_watch_state(task_id, {key: state[key] for key in ("folder", "match", "seen", "held")}, now)
        # Held files need no fast polling: a run ending changes the store, which wakes the watcher.
        return bool(pending)

    def _batch(self, task_id: str, definition: Dict[str, Any], schedule: Dict[str, Any], state: Dict[str, Any],
               ready: List[str], running: bool, now: float) -> bool:
        """One run for the files that arrived together, once they are all written and the folder is quiet."""
        pending: Dict[str, List[float]] = state["pending"]
        if running or not ready or len(ready) < len(pending):
            return False  # a run is going, or some file is still being written: wait for the whole group
        if now - max(pending[name][-1] for name in ready) < BATCH_QUIET_S:
            return False
        group = ready[:MAX_BATCH_FILES]
        if store.file_runs_since(task_id, now - 3600) >= MAX_RUNS_PER_HOUR:
            store.record_skip(task_id, scheduled_for=now, reason=REASON_CAPPED, count=len(group), now=now,
                              trigger="file", trigger_file=os.path.join(schedule["folder"], group[0]))
        elif self._start_batch(task_id, definition, [os.path.join(schedule["folder"], name) for name in group]) != "started":
            return False
        for name in group:
            state["seen"][name] = pending.pop(name)[:-1]
        return True

    @staticmethod
    def _own_output(running: bool, window: Optional[tuple], mtime: float, now: float) -> bool:
        if running:
            return True
        if not window:
            return False
        started, finished = window
        finished = finished or now
        return now - finished < QUIET_AFTER_RUN_S or started - 1 <= mtime <= finished + QUIET_AFTER_RUN_S

    # The thread

    def poke(self) -> None:
        self._dirty = True
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    def begin(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        self._thread = threading.Thread(target=self._loop, name="mac-mcp-folder-triggers", daemon=True)
        self._thread.start()

    def end(self) -> None:
        self._stop.set()
        self.poke()

    def _loop(self) -> None:
        queue = select.kqueue()
        queue.control([select.kevent(self._wake_r, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)
        watched: Dict[str, int] = {}
        flags = select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_EXTEND
        try:
            while not self._stop.is_set():
                try:
                    folders = {task["definition"]["schedule"]["folder"] for task in store.folder_tasks()}
                    for folder in [folder for folder in watched if folder not in folders]:
                        os.close(watched.pop(folder))
                    for folder in folders - set(watched):
                        try:
                            fd = os.open(folder, getattr(os, "O_EVTONLY", os.O_RDONLY))
                        except OSError:
                            continue  # tried again on the next pass
                        watched[folder] = fd
                        queue.control([select.kevent(fd, select.KQ_FILTER_VNODE, select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                                     flags)], 0)
                    self._dirty = False
                    settling = self.check()
                except Exception:
                    log.exception("folder triggers: pass failed")
                    settling = False
                events = queue.control(None, 16, PENDING_POLL_S if settling else SAFETY_SCAN_S)
                for event in events:
                    if event.ident == self._wake_r:
                        try:
                            while os.read(self._wake_r, 64):
                                pass
                        except (BlockingIOError, OSError):
                            pass
                    elif event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                        for folder, fd in list(watched.items()):
                            if fd == event.ident:
                                os.close(watched.pop(folder))  # reopened if it comes back
        finally:
            for fd in watched.values():
                os.close(fd)
            queue.close()
            for fd in (self._wake_r, self._wake_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._wake_r = self._wake_w = -1
