from finlab.db.conn import write_conn
from finlab.engine.control import drain_requests, request_refresh


def test_refresh_inbox_does_not_need_a_second_writer(initialised):
    with write_conn(initialised) as con:
        identity = request_refresh(initialised.data_dir)
        assert drain_requests(con, initialised.data_dir) == 1
        assert drain_requests(con, initialised.data_dir) == 0
        assert con.execute("SELECT job_type FROM job").fetchall() == [("prices",)]
        assert (initialised.data_dir / "control/requests" / f"{identity}.json").exists()


def test_bad_request_is_quarantined_without_stopping_the_writer(initialised):
    inbox = initialised.data_dir / "control/inbox"
    inbox.mkdir(parents=True)
    (inbox / "invalid.json").write_text("not json")
    with write_conn(initialised) as con:
        assert drain_requests(con, initialised.data_dir) == 0
    assert (initialised.data_dir / "control/rejected/invalid.json").exists()
