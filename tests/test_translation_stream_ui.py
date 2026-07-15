from src.ui.translation_controller import TkUpdateCoalescer, TranslationController


class FakeRoot:
    def __init__(self):
        self.callback = None
        self.cancelled = []

    def after(self, _interval_ms, callback):
        self.callback = callback
        return "after-1"

    def after_cancel(self, after_id):
        self.cancelled.append(after_id)

    def flush(self):
        callback = self.callback
        self.callback = None
        callback()


def test_coalescer_unpacks_render_arguments():
    root = FakeRoot()
    coalescer = TkUpdateCoalescer(root, interval_ms=40)
    rendered = []

    def render(progress, batch_data):
        rendered.append((progress, batch_data))

    coalescer.submit(render, 25, {"streaming": True})
    root.flush()

    assert rendered == [(25, {"streaming": True})]


def test_coalescer_keeps_latest_complete_snapshot():
    root = FakeRoot()
    coalescer = TkUpdateCoalescer(root, interval_ms=40)
    rendered = []

    def render(progress, batch_data):
        rendered.append((progress, batch_data))

    coalescer.submit(render, 10, {"preview_lines": ["你"]})
    coalescer.submit(render, 20, {"preview_lines": ["你好"]})
    root.flush()

    assert rendered == [(20, {"preview_lines": ["你好"]})]


def test_coalescer_keeps_concurrent_batches_separate():
    root = FakeRoot()
    coalescer = TkUpdateCoalescer(root, interval_ms=40)
    rendered = []

    coalescer.submit(rendered.append, "batch-1-old", coalesce_key=0)
    coalescer.submit(rendered.append, "batch-2", coalesce_key=20)
    coalescer.submit(rendered.append, "batch-1-new", coalesce_key=0)
    root.flush()

    assert rendered == ["batch-1-new", "batch-2"]


def test_finishing_one_batch_does_not_cancel_another_snapshot():
    root = FakeRoot()
    coalescer = TkUpdateCoalescer(root, interval_ms=40)
    rendered = []

    coalescer.submit(rendered.append, "batch-1", coalesce_key=0)
    coalescer.submit(rendered.append, "batch-2", coalesce_key=20)
    coalescer.cancel_pending(coalesce_key=20)
    root.flush()

    assert rendered == ["batch-1"]


def test_only_earliest_unfinished_batch_controls_scrolling():
    assert TranslationController._should_follow_stream({
        "batch_start": 0,
        "display_batch_start": 0,
    })
    assert not TranslationController._should_follow_stream({
        "batch_start": 20,
        "display_batch_start": 0,
    })


def test_stream_snapshot_prefers_cumulative_preview():
    batch_data = {
        "preview_lines": ["第一行", "正在生成"],
        "stream_lines": ["正在生成"],
        "stream_start_line": 1,
    }

    assert TranslationController._get_stream_snapshot(batch_data) == (
        ["第一行", "正在生成"],
        0,
    )
