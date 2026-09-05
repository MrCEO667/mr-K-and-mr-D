"""Structured logging: context must survive, or the logs are decoration."""
import json
import logging

from radar import log


def capture(caplog, fn):
    log.reset_for_tests()
    with caplog.at_level(logging.INFO):
        fn()
    return caplog.records


def test_bound_context_lands_on_every_record(caplog):
    records = capture(caplog, lambda: log.get("t", run_id="abc").info("hello"))
    assert records[0].run_id == "abc"


def test_call_site_extra_is_not_swallowed_by_bound_context(caplog):
    # LoggerAdapter.process replaces extra by default; losing the per-call
    # fields would leave "source down" lines with no source on them.
    records = capture(
        caplog,
        lambda: log.get("t", run_id="abc").warning("degraded", extra={"source": "tikwm"}),
    )
    assert records[0].run_id == "abc"
    assert records[0].source == "tikwm"


def test_formatter_emits_one_json_object_with_the_context():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "run started", None, None)
    record.run_id = "abc"
    parsed = json.loads(log.JsonFormatter().format(record))
    assert parsed["msg"] == "run started"
    assert parsed["run_id"] == "abc"
    assert parsed["level"] == "INFO"
