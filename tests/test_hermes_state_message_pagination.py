from hermes_state import SessionDB


def test_get_messages_tail_pagination(_isolate_hermes_home):
    db = SessionDB()
    try:
        db.create_session(session_id="s1", source="cli")
        for i in range(6):
            db.append_message(session_id="s1", role="user", content=f"m{i}")

        assert [m["content"] for m in db.get_messages("s1", limit=2, offset=0)] == ["m4", "m5"]
        assert [m["content"] for m in db.get_messages("s1", limit=2, offset=2)] == ["m2", "m3"]
        assert [m["content"] for m in db.get_messages("s1", limit=10, offset=0)] == [f"m{i}" for i in range(6)]
    finally:
        db.close()
