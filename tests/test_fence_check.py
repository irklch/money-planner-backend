from app.jobs.fence_check import FENCED, OK, check

ENV = {"FENCE_DOMAIN": "api.example.test", "FENCE_EXPECTED_IP": "203.0.113.10"}


def resolver_to(*ips):
    return lambda _domain: set(ips)


def failing_resolver(_domain):
    raise OSError("no dns")


def test_primary_starts():
    assert check(ENV, resolver_to("203.0.113.10"))[0] == OK


def test_old_server_after_cutover_is_fenced():
    code, reason = check(ENV, resolver_to("198.51.100.20"))
    assert code == FENCED and "not primary" in reason


def test_dns_outage_does_not_block_primary():
    assert check(ENV, failing_resolver)[0] == OK


def test_off_mode_for_pre_cutover_verification():
    assert check({**ENV, "FENCE_MODE": "off"}, resolver_to("198.51.100.20"))[0] == OK


def test_misconfiguration_fails_closed():
    assert check({"FENCE_MODE": "enforce"}, resolver_to("203.0.113.10"))[0] == FENCED
    assert check({**ENV, "FENCE_MODE": "maybe"}, resolver_to("203.0.113.10"))[0] == FENCED
