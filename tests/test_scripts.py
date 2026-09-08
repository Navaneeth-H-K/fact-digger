"""The local runner must wait longer than the server's batch budget."""

from scripts.run_local import request_timeout


def test_runner_timeout_exceeds_the_batch_budget() -> None:
    assert request_timeout(budget_s=600) >= 600 + 120
    assert request_timeout(budget_s=45) >= 45 + 120
