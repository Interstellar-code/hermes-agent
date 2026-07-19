def test_collect_scopes_profile_and_continues_from_last_offset(tmp_path, monkeypatch):
    import _db
    import _metrics

    home = tmp_path / "profile"
    logs = home / "logs"
    logs.mkdir(parents=True)
    log = logs / "agent.log"
    log.write_text("ERROR first\n", encoding="utf-8")

    db = _db.open_db(tmp_path / "metrics.db")
    monkeypatch.setattr(_db, "get_db", lambda: db)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda _profile: home)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _profile: True)

    first = _metrics.collect_profile_metrics(profile="neo")[0]
    with log.open("a", encoding="utf-8") as stream:
        stream.write("WARNING second\n")
    second = _metrics.collect_profile_metrics(profile="neo")[0]

    assert first["profile"] == "neo"
    assert first["error_count"] == 1
    assert second["from_offset"] == first["to_offset"]
    assert second["error_count"] == 0
    assert second["warn_count"] == 1
