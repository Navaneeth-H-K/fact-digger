"""The local runner must wait longer than the server's batch budget."""

from scripts.run_local import request_timeout


def test_runner_timeout_exceeds_the_batch_budget() -> None:
    # one page may wait through several rate-limit pauses and cap retries after the deadline
    assert request_timeout(budget_s=300) >= 300 + 1200
    assert request_timeout(budget_s=45) >= 45 + 1200
