import asyncio
from unittest.mock import patch

from django.test import TestCase

from apps.core.models import NvidiaApiKey, Proxy
from services.load_balancer import Route
from services import race_engine
from services.race_engine import (
    AllRoutesFailed, RaceResult, is_valid_response, is_valid_stream_chunk,
)


def make_route(name, key_suffix):
    key = NvidiaApiKey(name=f"key-{key_suffix}", api_key=f"nvapi-{key_suffix}")
    return Route(kind="direct", key=key)


def resp(obj):
    data = {
        "id": "chatcmpl-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return RaceResult(ok=True, route=obj, payload=data, http_status=200)


class ValidationTests(TestCase):
    def test_valid_response(self):
        self.assertTrue(is_valid_response(200, {"choices": [{"message": {"content": "hi"}}]}))
        self.assertFalse(is_valid_response(200, {"choices": []}))
        self.assertFalse(is_valid_response(200, {"error": {"message": "x"}}))
        self.assertFalse(is_valid_response(500, {"choices": [{"message": {"content": "hi"}}]}))

    def test_stream_chunk(self):
        self.assertIsNone(is_valid_stream_chunk("data: garbage"))
        self.assertIsNotNone(is_valid_stream_chunk(
            'data: {"choices":[{"delta":{"content":"h"}}]}'))
        self.assertEqual(is_valid_stream_chunk("data: [DONE]"), {"done": True})
        self.assertIsNone(is_valid_stream_chunk('data: {"error":{"message":"x"}}'))


class RaceTests(TestCase):
    def _race(self, behaviors):
        """behaviors: list of (delay, ok). Returns (winner_index, cancelled_flags)."""
        cancelled = [False] * len(behaviors)
        delay_slow = 60.0

        async def fake_do_request(route, body, base_url, started=None):
            idx = int(route.key.name.split("-")[-1])
            delay, ok = behaviors[idx]
            try:
                await asyncio.sleep(delay)
                if ok:
                    r = resp(route)
                    return r
                return RaceResult(ok=False, route=route, error_type="boom", http_status=500)
            except asyncio.CancelledError:
                cancelled[idx] = True
                raise

        with patch.object(race_engine, "_do_request", fake_do_request), \
             patch.object(race_engine, "_mark_success"), \
             patch.object(race_engine, "_mark_failure"):
            routes = [make_route(f"r{i}", i) for i in range(len(behaviors))]
            try:
                result = asyncio.run(race_engine._race(routes, {}, "http://upstream"))
                winner_idx = int(result.route.key.name.split("-")[-1])
                return winner_idx, cancelled, result
            except AllRoutesFailed:
                return -1, cancelled, None

    def test_fast_wins_slow_cancelled(self):
        # A slow (1.5s), B fast (0.05s), C fails
        winner, cancelled, result = self._race([(1.5, True), (0.05, True), (0.01, False)])
        self.assertEqual(winner, 1)
        self.assertTrue(cancelled[0])

    def test_failures_skipped_until_success(self):
        winner, cancelled, result = self._race([(0.01, False), (0.02, False), (0.05, True)])
        self.assertEqual(winner, 2)

    def test_all_failed_raises(self):
        winner, cancelled, result = self._race([(0.01, False), (0.01, False)])
        self.assertEqual(winner, -1)


class StreamRaceTests(TestCase):
    def test_first_valid_stream_wins_rest_cancelled(self):
        from services import race_engine

        cancelled = [False, False, False]

        async def fake_stream(route, body, base_url):
            idx = int(route.key.name.split("-")[-1])
            delay = [0.5, 0.05, 0.3][idx]
            ok = [True, True, False][idx]
            try:
                await asyncio.sleep(delay)
                if not ok:
                    return None
                # cm, req_cm, resp, aiter, first_line
                return (None, None, None, None, 'data: {"choices":[{"delta":{"content":"h"}}]}')
            except asyncio.CancelledError:
                cancelled[idx] = True
                raise

        with patch.object(race_engine, "_stream_first_valid", fake_stream):
            routes = [make_route(f"r{i}", i) for i in range(3)]
            route, *_ = asyncio.run(
                race_engine.race_stream_winner(routes, {}, "http://upstream"))
            self.assertEqual(route.key.name, "key-1")
        self.assertTrue(cancelled[0])
        self.assertTrue(cancelled[2])

    def test_stream_all_invalid_raises(self):
        from services import race_engine

        async def fake_stream(route, body, base_url):
            return None

        with patch.object(race_engine, "_stream_first_valid", fake_stream):
            routes = [make_route("r0", 0)]
            with self.assertRaises(AllRoutesFailed):
                asyncio.run(race_engine.race_stream_winner(routes, {}, "http://x"))
