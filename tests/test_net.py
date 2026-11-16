import unittest

from pipeline import config, net


class FakeClock(object):
    """Time only moves when someone sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def scripted(responses):
    """Transport that replays (status, body) tuples or raises exceptions; records calls."""
    calls = []
    queue = list(responses)

    def transport(url, headers, timeout):
        calls.append((url, dict(headers)))
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item[0], item[1], {}

    transport.calls = calls
    return transport


def make(responses, deadline=None):
    fake = FakeClock()
    transport = scripted(responses)
    client = net.Client(transport=transport, clock=fake.clock, sleep=fake.sleep, deadline=deadline)
    return client, transport, fake


URL = "https://query1.finance.yahoo.com/v8/finance/chart/A"


class ClientTest(unittest.TestCase):
    def test_success_sends_honest_headers(self):
        client, transport, _ = make([(200, b"{}")])
        resp = client.get(URL)
        self.assertTrue(resp.ok)
        self.assertEqual(resp.body, b"{}")
        headers = transport.calls[0][1]
        self.assertEqual(headers["User-Agent"], config.USER_AGENT)
        self.assertNotIn("Mozilla", headers["User-Agent"])   # never poses as a browser
        self.assertNotIn("Cookie", headers)

    def test_not_found_is_final_and_not_an_outage(self):
        client, transport, _ = make([(404, b"nope")])
        resp = client.get(URL)
        self.assertEqual(resp.kind, net.NOT_FOUND)
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(client.host("yahoo").is_open)

    def test_requests_are_paced_per_host(self):
        client, _, fake = make([(200, b"{}")])
        for _ in range(4):
            client.get(URL)
        self.assertAlmostEqual(sum(fake.slept), 3 * config.HOST_INTERVALS["yahoo"], places=5)

    def test_query1_and_query2_share_one_budget(self):
        client, _, _ = make([(200, b"{}")])
        client.get(URL)
        client.get("https://query2.finance.yahoo.com/v1/finance/search?q=A")
        self.assertEqual(client.stats()["yahoo"]["requests"], 2)

    def test_rate_limit_pauses_then_recovers(self):
        client, transport, fake = make([(429, b"Too Many Requests"), (200, b"{}")])
        resp = client.get(URL)
        self.assertTrue(resp.ok)
        self.assertEqual(len(transport.calls), 2)
        self.assertGreaterEqual(max(fake.slept), config.BLOCK_PAUSE_BASE_S)
        host = client.host("yahoo")
        self.assertEqual(host.strikes, 0)                       # success clears the strike count
        self.assertGreater(host.interval, host.base_interval)   # but stays slower for a while
        self.assertEqual(client.stats()["yahoo"]["blocked"], 1)

    def test_persistent_refusal_opens_the_breaker_and_stops_calling(self):
        client, transport, _ = make([(429, b"Too Many Requests")])
        kinds = [client.get(URL).kind for _ in range(6)]
        self.assertIn(net.BLOCKED, kinds)
        self.assertEqual(kinds[-1], net.UNAVAILABLE)
        self.assertTrue(client.host("yahoo").is_open)
        sent = len(transport.calls)
        self.assertLessEqual(sent, config.BLOCK_MAX_STRIKES + 1)
        again = client.get(URL)                                  # no further traffic once open
        self.assertEqual(again.kind, net.UNAVAILABLE)
        self.assertEqual(len(transport.calls), sent)
        self.assertIn("refusing", again.describe())

    def test_a_blocked_host_does_not_affect_another_host(self):
        client, _, _ = make([(429, b"")])
        for _ in range(6):
            client.get(URL)
        self.assertTrue(client.host("yahoo").is_open)
        self.assertFalse(client.host("nasdaq").is_open)

    def test_network_errors_retry_then_fail(self):
        client, transport, _ = make([OSError("connection reset")])
        resp = client.get(URL)
        self.assertEqual(resp.kind, net.ERROR)
        self.assertEqual(len(transport.calls), 3)                # first try + two retries
        self.assertIn("OSError", resp.describe())

    def test_network_error_then_success(self):
        client, transport, _ = make([OSError("timeout"), (200, b"ok")])
        self.assertTrue(client.get(URL).ok)
        self.assertEqual(len(transport.calls), 2)

    def test_server_errors_retry(self):
        client, transport, _ = make([(503, b""), (503, b""), (200, b"ok")])
        self.assertTrue(client.get(URL).ok)
        self.assertEqual(len(transport.calls), 3)

    def test_many_consecutive_errors_open_the_breaker(self):
        client, _, _ = make([OSError("down")])
        for _ in range(config.ERROR_MAX_CONSECUTIVE):
            client.get(URL)
        self.assertTrue(client.host("yahoo").is_open)
        self.assertEqual(client.get(URL).kind, net.UNAVAILABLE)

    def test_time_budget_stops_new_requests(self):
        client, transport, fake = make([(200, b"{}")], deadline=1000.5)
        self.assertTrue(client.get(URL).ok)
        fake.now = 2000.0
        resp = client.get(URL)
        self.assertEqual(resp.kind, net.UNAVAILABLE)
        self.assertIn("time budget", resp.describe())
        self.assertEqual(len(transport.calls), 1)

    def test_concurrent_refusals_count_as_one_strike(self):
        client, _, _ = make([(200, b"{}")])
        host = client.host("yahoo")
        epoch = host.wait_turn()
        host.on_blocked(epoch)
        host.on_blocked(epoch)      # a second in-flight request reporting the same episode
        host.on_blocked(epoch)
        self.assertEqual(host.strikes, 1)


if __name__ == "__main__":
    unittest.main()
