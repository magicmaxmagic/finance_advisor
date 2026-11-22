from finlab.engine.status import IngestionStatus, IngestionStep, read_status, write_status


def test_status_keeps_bounded_completed_job_attempts_without_duplicates(tmp_path):
    assert read_status(tmp_path)["pipeline_state"] == "not_started"
    for index in range(52):
        step = IngestionStep(
            "prices", "done", str(index), str(index + 1), 1, None, 1, 0, 0, "complete"
        )
        record = IngestionStatus(str(index + 1), "done", [step])
        write_status(tmp_path, record)
        write_status(tmp_path, record)
    step = IngestionStep("prices", "running", "current", None, None, None, 0, 0, 0, "working")
    write_status(tmp_path, IngestionStatus("current", "running", [step]))
    details = read_status(tmp_path)
    assert details["steps"][0]["state"] == "running"
    assert len(details["recent_jobs"]) == 50
    assert details["recent_jobs"][0]["started_at"] == "2"
    assert details["recent_jobs"][-1]["started_at"] == "51"
