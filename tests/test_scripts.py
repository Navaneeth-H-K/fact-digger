"""The local runner must wait longer than the server's batch budget."""

from scripts.run_local import request_timeout


def test_runner_timeout_exceeds_the_batch_budget() -> None:
    # one page may wait through several rate-limit pauses and cap retries after the deadline
    assert request_timeout(budget_s=300) >= 300 + 1200
    assert request_timeout(budget_s=45) >= 45 + 1200


def test_process_loop_sends_retry_flag_once_and_stops_when_done() -> None:
    import httpx

    from scripts.run_local import process

    calls: list[str] = []
    responses = iter(
        [
            {
                "done": 1,
                "failed": 0,
                "skipped": 0,
                "pending": 1,
                "processed_this_call": 1,
                "estimated_calls_remaining": 1,
                "last_errors": [],
                "status": "processing",
            },
            {
                "done": 2,
                "failed": 0,
                "skipped": 0,
                "pending": 0,
                "processed_this_call": 1,
                "estimated_calls_remaining": 0,
                "last_errors": [],
                "status": "extracted",
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=next(responses))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    progress = process(client, "http://t", "doc-1", budget_s=10, retry_failed=True)
    assert progress["status"] == "extracted" and len(calls) == 2
    assert "retry_failed=true" in calls[0] and "retry_failed" not in calls[1]
