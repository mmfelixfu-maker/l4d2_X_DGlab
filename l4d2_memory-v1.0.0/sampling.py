"""One blocking sampler thread; only new snapshots wake the asyncio bridge."""
from __future__ import annotations

from pathlib import Path
import threading
import time

from native import ReadError
from reader import ProfileError, Reader, TickChanged


class Sampler:
    def __init__(self, notify, root, config, revision, reader_factory=Reader):
        self.notify, self.root, self.reader_factory = notify, Path(root), reader_factory
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.config, self.revision = dict(config), revision
        self.discarded = 0
        self.polls = 0
        self.full_snapshots = 0
        self.thread = threading.Thread(target=self._run, name="l4d2-reader", daemon=True)

    def configure(self, config, revision):
        with self.lock:
            self.config, self.revision = dict(config), revision
        self.wake.set()

    def start(self): self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def join(self): self.thread.join()

    def _run(self):
        reader, source, revision = None, None, -1
        last_error, last_progress, stale = None, time.monotonic(), False
        try:
            while not self.stopping.is_set():
                started = time.monotonic()
                with self.lock:
                    cfg, current_revision = self.config, self.revision
                    self.wake.clear()
                requested_source = (cfg["profile_path"], cfg["temp_decay"])
                if source != requested_source:
                    if reader: reader.close()
                    reader = None
                    source = requested_source
                if revision != current_revision:
                    revision = current_revision
                    last_error, last_progress, stale = None, started, False
                    if reader:
                        reader.last_marker = None
                        reader.last_snapshot = None
                        reader.last_full_read = 0.0
                delay = 1 / cfg["poll_hz"]
                try:
                    if reader is None:
                        path = Path(cfg["profile_path"])
                        if not path.is_absolute(): path = self.root / path
                        reader = self.reader_factory(path, cfg["temp_decay"])
                    self.polls += 1
                    snapshot = reader.sample(only_changed=True)
                    if snapshot is not None:
                        self.full_snapshots += 1
                        last_progress, last_error, stale = started, None, False
                        self.notify("sample", revision, snapshot)
                    elif started - last_progress > cfg["stale_s"]:
                        if not stale:
                            self.notify("error", revision, ("游戏数据停止更新，已暂停输出", "pending"))
                            stale = True
                        # A paused game does not need 120 marker reads a second.
                        delay = max(delay, 0.1)
                except TickChanged:
                    self.discarded += 1
                except (ReadError, ValueError, OSError) as exc:
                    detail = str(exc)
                    state = "warn" if isinstance(exc, (ProfileError, ValueError)) else "pending"
                    if (detail, state) != last_error:
                        self.notify("error", revision, (detail, state))
                        last_error = (detail, state)
                    # Retain an attached process across menu/loading transitions;
                    # don't enumerate modules and rehash the DLL every retry.
                    if reader and reader.process is not None and not reader.process.alive():
                        reader.close()
                    delay = 2.0 if state == "warn" else 1.0
                self.wake.wait(max(0.001, delay - (time.monotonic() - started)))
        except BaseException as exc:
            self.notify("fatal", revision, str(exc))
        finally:
            if reader: reader.close()
