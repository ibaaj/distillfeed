from rss_reader.operations import operation_message


def test_refresh_operation_message_distinguishes_source_deferral_from_failure():
    message = operation_message("refresh", {
        "status": "partial",
        "attempted": 4,
        "succeeded": 3,
        "deferred": 1,
        "failed": 0,
        "new_items": 12,
    })
    assert message == (
        "Checked 4 feeds; 3 succeeded, 1 deferred by source or retry policy and "
        "12 new entries were stored. Successful feeds kept their updates."
    )
