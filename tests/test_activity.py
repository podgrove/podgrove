from podgrove.activity import WatchActivity


def test_watch_only_changes_refresh_activity_with_no_bind_sync(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    source = content / "app.py"
    source.write_text("old\n")
    model = {"services": {"app": {"develop": {"watch": [{"path": str(content), "action": "sync", "target": "/app"}]}}}}
    activity = WatchActivity(model)
    assert not activity.changed()
    source.write_text("new source\n")
    assert activity.changed()
    assert not activity.changed()
    source.rename(content / "renamed.py")
    assert activity.changed()
    (content / "renamed.py").unlink()
    assert activity.changed()


def test_git_metadata_alone_is_not_worktree_activity(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "index").write_text("before")
    activity = WatchActivity({"services": {"app": {"develop": {"watch": [{"path": str(tmp_path)}]}}}})
    (gitdir / "index").write_text("after with different length")
    assert not activity.changed()
