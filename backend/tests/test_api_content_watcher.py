"""Tests for api/content_watcher.py — visualization_changed / memory_changed (spec 12 §9.3)."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from api.content_watcher import ContentWatcher, _merge_kind, affected_visualizations, is_ignored
from api.indexer import MemoryWatcher

CREATED, MODIFIED, DELETED = 1, 2, 3  # watchfiles.Change values


class TestIsIgnored:
    @pytest.mark.parametrize("path", [
        "visualizations/.foo.html.Xa91Bc",   # rsync temp
        "viz/.#index.html",                  # emacs lock
        "viz/index.html.swp",
        "viz/index.html~",
        "viz/index.html.tmp.1234.1700000000",  # atomic-write temp
        "viz/data.json.part",
        "viz/4913",                          # vim write probe
        "viz/node_modules/x/index.js",
        "viz/.git/HEAD",
        ".hidden/index.html",
    ])
    def test_ignored(self, path):
        assert is_ignored(path)

    @pytest.mark.parametrize("path", [
        "visualizations/foo.html",
        "avatar-pipeline/data/state.json",
        "projects/x.md",
        "tarot-canvas/assets/card-01.png",
        "temperature.html",
    ])
    def test_kept(self, path):
        assert not is_ignored(path)


class TestMergeKind:
    def test_replace_is_modified(self):
        assert _merge_kind("deleted", "created") == "modified"

    def test_create_then_modify_is_created(self):
        assert _merge_kind("created", "modified") == "created"

    def test_delete_wins(self):
        assert _merge_kind("modified", "deleted") == "deleted"


def _write(root: Path, rel: str, text: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class TestAffectedVisualizations:
    def test_html_is_itself(self, tmp_path):
        assert affected_visualizations(tmp_path, "visualizations/foo.html") == ["visualizations/foo.html"]

    def test_asset_in_folder_maps_to_pages_that_name_it(self, tmp_path):
        _write(tmp_path, "avatar/index.html", "<script src='app.js'></script>")
        _write(tmp_path, "avatar/observer.html", "fetch('data/state.json')")
        _write(tmp_path, "avatar/rig.html", "nothing here")
        assert affected_visualizations(tmp_path, "avatar/data/state.json") == ["avatar/observer.html"]

    def test_asset_named_by_no_page_falls_back_to_nearest_index(self, tmp_path):
        _write(tmp_path, "tarot/index.html", "<script src='js/app.js'></script>")
        assert affected_visualizations(tmp_path, "tarot/cards/data/deck.json") == ["tarot/index.html"]

    def test_root_level_asset_only_searches_root_pages(self, tmp_path):
        _write(tmp_path, "markdown_reader.html", "<script src='marked.min.js'></script>")
        _write(tmp_path, "viz/index.html", "<script src='../marked.min.js'></script>")
        assert affected_visualizations(tmp_path, "marked.min.js") == ["markdown_reader.html"]

    def test_folder_asset_never_reaches_unrelated_root_pages(self, tmp_path):
        _write(tmp_path, "other.html", "app.js")
        _write(tmp_path, "viz/index.html", "<script src='lib.js'></script>")
        assert affected_visualizations(tmp_path, "viz/app.js") == ["viz/index.html"]

    def test_name_match_is_boundary_aware(self, tmp_path):
        _write(tmp_path, "v/index.html", "<script src='data.js'></script><script src='a.json'></script>")
        _write(tmp_path, "v/other.html", "<script src=\"./a.js\"></script>")
        assert affected_visualizations(tmp_path, "v/a.js") == ["v/other.html"]

    def test_unrelated_asset_is_none(self, tmp_path):
        _write(tmp_path, "visualizations/a.html", "x")
        assert affected_visualizations(tmp_path, "videos/clip.mp4") == []


class TestClassify:
    def test_splits_public_and_memory(self, tmp_path):
        public = tmp_path / "public"
        memory = tmp_path / "memory"
        docs = tmp_path / "docs"
        for d in (public, memory, docs):
            d.mkdir()
        _write(public, "viz/index.html")
        _write(memory, "projects/x.md")
        _write(docs, "specs/12.md")
        roots = [(memory, ""), (docs, "archie/")]
        changes = {
            (MODIFIED, str(public / "viz" / "index.html")),
            (CREATED, str(public / "viz" / ".index.html.Ab12Cd")),
            (MODIFIED, str(memory / "projects" / "x.md")),
            (MODIFIED, str(memory / "projects" / "notes.txt")),
            (DELETED, str(docs / "specs" / "old.md")),
            (MODIFIED, str(docs / "specs" / "12.md")),
        }
        pub, mem = ContentWatcher.classify(changes, public, roots)
        assert pub == {"viz/index.html": "modified"}
        assert mem == {"projects/x.md": "modified", "archie/specs/12.md": "modified", "archie/specs/old.md": "deleted"}

    def test_rsync_rename_reports_the_target(self, tmp_path):
        public = tmp_path / "public"
        _write(public, "v/a.html")
        changes = [
            (CREATED, str(public / "v" / ".a.html.Q1w2E3")),
            (DELETED, str(public / "v" / ".a.html.Q1w2E3")),
            (CREATED, str(public / "v" / "a.html")),
        ]
        pub, mem = ContentWatcher.classify(changes, public, [])
        assert pub == {"v/a.html": "created"}
        assert mem == {}

    def test_deleted_folder_is_skipped_its_files_are_not(self, tmp_path):
        public = tmp_path / "public"
        public.mkdir()
        pub, _ = ContentWatcher.classify(
            [(DELETED, str(public / "old")), (DELETED, str(public / "old" / "index.html"))], public, [],
        )
        assert pub == {"old/index.html": "deleted"}

    def test_directory_events_are_skipped(self, tmp_path):
        public = tmp_path / "public"
        (public / "newdir").mkdir(parents=True)
        pub, _ = ContentWatcher.classify([(CREATED, str(public / "newdir"))], public, [])
        assert pub == {}


class TestVisualizationFrame:
    def test_asset_and_page_kinds(self, tmp_path):
        _write(tmp_path, "dash/index.html", "<script src='app.js'></script>")
        _write(tmp_path, "dash/app.js", "fetch('data.json')")
        _write(tmp_path, "new.html")
        frame = ContentWatcher.visualization_frame(
            tmp_path, {"dash/app.js": "modified", "dash/data.json": "modified", "new.html": "created"},
        )
        assert frame == {
            "type": "visualization_changed",
            "visualizations": [
                {"path": "dash/index.html", "kind": "modified"},
                {"path": "new.html", "kind": "created"},
            ],
            "files": [
                {"path": "dash/app.js", "kind": "modified"},
                {"path": "dash/data.json", "kind": "modified"},
                {"path": "new.html", "kind": "created"},
            ],
        }

    def test_own_delete_beats_asset_modified(self, tmp_path):
        _write(tmp_path, "v/style.css")
        frame = ContentWatcher.visualization_frame(tmp_path, {"v/index.html": "deleted", "v/style.css": "modified"})
        assert frame["visualizations"] == [{"path": "v/index.html", "kind": "deleted"}]

    def test_empty_batch_has_no_frame(self, tmp_path):
        assert ContentWatcher.visualization_frame(tmp_path, {}) is None


class TestRoots:
    def test_memory_alias_from_symlink(self, tmp_path):
        context = tmp_path / "context"
        (context / "public").mkdir(parents=True)
        (context / "memory").mkdir()
        docs = tmp_path / "docs"
        docs.mkdir()
        (context / "memory" / "archie").symlink_to(docs)
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            public, memory = ContentWatcher.roots()
        assert public == (context / "public").resolve()
        assert memory == [((context / "memory").resolve(), ""), (docs.resolve(), "archie/")]


class TestRun:
    @pytest.mark.asyncio
    async def test_pushes_frames_and_wakes_the_indexer(self, tmp_path, monkeypatch):
        # Polling: the test must not depend on the machine's inotify budget.
        monkeypatch.setenv("WATCHFILES_FORCE_POLLING", "1")
        monkeypatch.setattr("api.content_watcher.POLL_DELAY_MS", 100)
        context = tmp_path / "context"
        public = context / "public"
        memory = context / "memory"
        _write(public, "dash/index.html", "<title>Dash</title><script src='data.json'></script>")
        memory.mkdir(parents=True)
        (tmp_path / "docs").mkdir()

        frames: list[dict] = []
        got = asyncio.Event()
        woken: list[bool] = []

        async def broadcast(frame: dict) -> None:
            frames.append(frame)
            if {f["type"] for f in frames} == {"visualization_changed", "memory_changed"}:
                got.set()

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            watcher = ContentWatcher(broadcast, on_memory_markdown=lambda: woken.append(True), debounce_ms=400, step_ms=50)
            task = asyncio.create_task(watcher.run())
            try:
                await asyncio.sleep(0.5)  # let the inotify watches settle
                _write(public, "dash/data.json", "{}")
                _write(public, "dash/.data.json.tmp9")
                _write(memory, "projects/x.md", "# x")
                await asyncio.wait_for(got.wait(), timeout=10)
            finally:
                watcher.stop()
                await asyncio.wait_for(task, timeout=10)

        viz = next(f for f in frames if f["type"] == "visualization_changed")
        assert {"path": "dash/index.html", "kind": "modified"} in viz["visualizations"]
        assert all(not f["path"].startswith("dash/.") for f in viz["files"])
        mem = next(f for f in frames if f["type"] == "memory_changed")
        assert mem["changes"] == [{"path": "projects/x.md", "kind": "created"}] or mem["changes"] == [
            {"path": "projects/x.md", "kind": "modified"}
        ]
        assert woken


    @pytest.mark.asyncio
    async def test_falls_back_to_polling_when_out_of_watches(self, tmp_path):
        (tmp_path / "context" / "public").mkdir(parents=True)
        calls: list = []

        def fake_awatch(*paths, force_polling=None, stop_event=None, **kwargs):
            calls.append(force_polling)

            async def gen():
                if not force_polling:
                    raise OSError("OS file watch limit reached. about [...]")
                stop_event.set()
                return
                yield  # pragma: no cover

            return gen()

        async def broadcast(_frame):
            pass

        with patch("utils.paths.PROJECT_ROOT", tmp_path), patch("watchfiles.awatch", fake_awatch):
            await asyncio.wait_for(ContentWatcher(broadcast).run(), timeout=2)
        assert calls == [None, True]


    @pytest.mark.asyncio
    async def test_a_root_that_appears_later_is_picked_up(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WATCHFILES_FORCE_POLLING", "1")
        monkeypatch.setattr("api.content_watcher.POLL_DELAY_MS", 100)
        monkeypatch.setattr("api.content_watcher.ROOTS_RECHECK_S", 0.3)
        (tmp_path / "context" / "public").mkdir(parents=True)
        frames: list[dict] = []
        got = asyncio.Event()

        async def broadcast(frame: dict) -> None:
            frames.append(frame)
            if frame["type"] == "memory_changed":
                got.set()

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            watcher = ContentWatcher(broadcast, debounce_ms=300, step_ms=50)
            task = asyncio.create_task(watcher.run())
            try:
                await asyncio.sleep(0.3)
                memory = tmp_path / "context" / "memory"
                memory.mkdir()
                await asyncio.sleep(1.0)  # the recheck restarts the watch with memory/
                _write(memory, "late.md", "# late")
                await asyncio.wait_for(got.wait(), timeout=10)
            finally:
                watcher.stop()
                await asyncio.wait_for(task, timeout=10)
        assert any(f["type"] == "memory_changed" and f["changes"][0]["path"] == "late.md" for f in frames)

    @pytest.mark.asyncio
    async def test_other_errors_retry_with_backoff(self, tmp_path, monkeypatch):
        (tmp_path / "context" / "public").mkdir(parents=True)
        monkeypatch.setattr("api.content_watcher.ERROR_BACKOFF_S", 0.01)
        calls: list[int] = []

        def fake_awatch(*paths, stop_event=None, **kwargs):
            calls.append(1)

            async def gen():
                if len(calls) < 3:
                    raise OSError("watched directory went away")
                stop_event.set()
                return
                yield  # pragma: no cover

            return gen()

        async def broadcast(_frame):
            pass

        with patch("utils.paths.PROJECT_ROOT", tmp_path), patch("watchfiles.awatch", fake_awatch):
            await asyncio.wait_for(ContentWatcher(broadcast).run(), timeout=2)
        assert len(calls) == 3


class TestMemoryIndexer:
    @pytest.mark.asyncio
    async def test_notify_runs_once_per_burst(self, tmp_path):
        runs: list[tuple] = []
        release = asyncio.Event()

        async def fake_index(project_dir, *args):
            runs.append(args)
            await release.wait()
            return True

        watcher = MemoryWatcher(tmp_path)
        with patch("api.indexer._run_index_script", side_effect=fake_index):
            task = asyncio.create_task(watcher.run())
            watcher.notify()
            await asyncio.sleep(0.05)
            # Three more batches while the first run is in flight → exactly one more run.
            watcher.notify()
            watcher.notify()
            watcher.notify()
            release.set()
            await asyncio.sleep(0.05)
            watcher.stop()
            await asyncio.wait_for(task, timeout=2)
        assert runs == [("--memory-only",), ("--memory-only",)]

    @pytest.mark.asyncio
    async def test_stop_without_changes(self, tmp_path):
        watcher = MemoryWatcher(tmp_path)
        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.01)
        watcher.stop()
        await asyncio.wait_for(task, timeout=2)
