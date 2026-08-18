"""RenderDoc-independent tests for bounded bridge request framing."""

from extension.bridge import MAX_REQUEST_BYTES, RequestFramer, protocol_error


def _valid_eval_line(total_bytes):
    prefix = b'{"cmd":"eval","params":{"code":"'
    suffix = b'"}}\n'
    padding = total_bytes - len(prefix) - len(suffix)
    assert padding >= 0
    return prefix + (b"x" * padding) + suffix


def test_framer_handles_partial_and_multiple_requests():
    framer = RequestFramer()

    assert framer.feed(b'{"cmd":"ev') == []
    results = framer.feed(
        b'al","params":{}}\n{"cmd":"api_index","params":{}}\n'
    )

    assert [result.request for result in results] == [
        {"cmd": "eval", "params": {}},
        {"cmd": "api_index", "params": {}},
    ]
    assert all(result.response is None for result in results)
    assert framer.buffered_bytes == 0


def test_framer_accepts_request_at_exact_byte_limit():
    line = _valid_eval_line(MAX_REQUEST_BYTES)
    framer = RequestFramer()
    results = []

    for offset in range(0, len(line), 65536):
        results.extend(framer.feed(line[offset:offset + 65536]))
        assert framer.buffered_bytes < MAX_REQUEST_BYTES

    assert len(results) == 1
    assert results[0].request["cmd"] == "eval"
    assert len(results[0].request["params"]["code"]) > 8_000_000
    assert results[0].close is False


def test_framer_rejects_oversized_request_without_retaining_buffer():
    line = _valid_eval_line(MAX_REQUEST_BYTES + 1)
    framer = RequestFramer()
    results = []

    for offset in range(0, len(line), 65536):
        results.extend(framer.feed(line[offset:offset + 65536]))
        assert framer.buffered_bytes < MAX_REQUEST_BYTES

    assert len(results) == 1
    assert results[0].response["error_code"] == "request_too_large"
    assert results[0].close is True
    assert framer.buffered_bytes == 0
    assert framer.feed(b'{"cmd":"eval"}\n') == []


def test_framer_recovers_after_utf8_json_and_object_errors():
    framer = RequestFramer()

    results = framer.feed(
        b'\xff\n{]\n[]\n{"cmd":"instance_info","params":{}}\n'
    )

    assert [result.response["error_code"] for result in results[:3]] == [
        "invalid_utf8",
        "invalid_json",
        "request_not_object",
    ]
    assert all(result.close is False for result in results[:3])
    assert results[3].request == {"cmd": "instance_info", "params": {}}


def test_framer_reports_partial_request_at_eof_and_clears_state():
    framer = RequestFramer()
    framer.feed(b'{"cmd":"eval"')

    results = framer.finish()

    assert len(results) == 1
    assert results[0].response == protocol_error(
        "incomplete_request",
        "connection closed before the request newline was received",
    )
    assert results[0].close is True
    assert framer.buffered_bytes == 0
    assert framer.finish() == []


def test_framer_clean_eof_has_no_error():
    assert RequestFramer().finish() == []
