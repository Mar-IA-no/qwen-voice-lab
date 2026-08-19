from __future__ import annotations

import threading
import time

from qwen_voice_lab.config import Settings
from qwen_voice_lab.service import JobManager
from qwen_voice_lab.storage import Store


def test_idle_unload_cannot_overlap_render(tmp_path) -> None:
    settings = Settings(engine="mock", data_dir=tmp_path / "data", host="127.0.0.1")
    settings.prepare()
    manager = JobManager(settings, Store(settings))
    rendering = threading.Event()
    release = threading.Event()
    unloaded = threading.Event()

    class BlockingEngine:
        def render_synthesis(self, *args):
            rendering.set()
            assert release.wait(3)

        def unload_if_idle(self, idle_seconds):
            unloaded.set()
            return True

    manager.engine = BlockingEngine()
    render = threading.Thread(
        target=manager._render_synthesis_locked,
        args=(None, None, None, None, None),
    )
    sweep = threading.Thread(target=manager._unload_if_idle_locked, args=(30,))
    render.start()
    assert rendering.wait(1)
    sweep.start()
    time.sleep(0.05)
    assert not unloaded.is_set()
    release.set()
    render.join(timeout=2)
    sweep.join(timeout=2)
    assert unloaded.is_set()
