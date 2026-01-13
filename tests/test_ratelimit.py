"""Tests for rate limiting functionality."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from twinops.common.ratelimit import (
    RateLimiter,
    RateLimitMiddleware,
    TokenBucket,
    create_rate_limit_middleware,
)


class TestTokenBucket:
    """Tests for TokenBucket rate limiter implementation."""

    def test_initial_capacity(self):
        """Bucket starts at full capacity."""
        bucket = TokenBucket(rate=1.0, capacity=10.0)
        assert bucket.tokens_available == 10.0

    def test_consume_success(self):
        """Consuming tokens succeeds when available."""
        bucket = TokenBucket(rate=1.0, capacity=10.0)
        assert bucket.consume(5.0) is True
        assert bucket.tokens_available == pytest.approx(5.0, abs=0.01)

    def test_consume_failure(self):
        """Consuming more tokens than available fails."""
        bucket = TokenBucket(rate=1.0, capacity=5.0)
        assert bucket.consume(10.0) is False
        # Tokens should remain unchanged
        assert bucket.tokens_available == 5.0

    def test_refill_over_time(self):
        """Tokens refill based on elapsed time."""
        # Use high rate to see refill quickly
        bucket = TokenBucket(rate=1000.0, capacity=10.0)

        # Consume all tokens
        bucket.consume(10.0)

        # Manually adjust last_update to simulate time passing
        # 0.005 seconds * 1000 tokens/sec = 5 tokens
        bucket._last_update = time.time() - 0.005
        tokens = bucket.tokens_available
        assert tokens == pytest.approx(5.0, abs=0.5)

    def test_refill_capped_at_capacity(self):
        """Tokens don't exceed capacity after refill."""
        bucket = TokenBucket(rate=100.0, capacity=10.0)

        # Manually adjust last_update to simulate long time passing
        bucket._last_update = time.time() - 10.0

        # Even after long time, tokens don't exceed capacity
        assert bucket.tokens_available == 10.0

    def test_time_until_available_zero_when_available(self):
        """time_until_available returns 0 when tokens are available."""
        bucket = TokenBucket(rate=1.0, capacity=10.0)
        assert bucket.time_until_available(5.0) == 0.0

    def test_time_until_available_calculates_deficit(self):
        """time_until_available calculates correct wait time."""
        bucket = TokenBucket(rate=10.0, capacity=10.0)

        # Consume all tokens
        bucket.consume(10.0)

        # Need 5 tokens at 10 tokens/sec = 0.5 seconds
        wait_time = bucket.time_until_available(5.0)
        assert wait_time == pytest.approx(0.5, abs=0.01)

    def test_consume_default_one_token(self):
        """consume() defaults to 1 token."""
        bucket = TokenBucket(rate=1.0, capacity=10.0)
        bucket.consume()
        assert bucket.tokens_available == pytest.approx(9.0, abs=0.1)


class TestRateLimiter:
    """Tests for per-client RateLimiter."""

    def test_creates_bucket_per_client(self):
        """Each client gets their own bucket."""
        limiter = RateLimiter(requests_per_minute=60.0)

        limiter.check("client1")
        limiter.check("client2")

        assert "client1" in limiter._buckets
        assert "client2" in limiter._buckets

    def test_check_returns_allowed(self):
        """check() returns True when request is allowed."""
        limiter = RateLimiter(requests_per_minute=60.0)

        allowed, retry_after = limiter.check("client1")
        assert allowed is True
        assert retry_after == 0.0

    def test_check_returns_denied_after_burst(self):
        """check() returns False after burst is exhausted."""
        # Very low limit: 1 request per minute, 2 burst
        limiter = RateLimiter(requests_per_minute=1.0, burst_size=2.0)

        # First two requests should succeed (burst)
        allowed1, _ = limiter.check("client1")
        allowed2, _ = limiter.check("client1")
        assert allowed1 is True
        assert allowed2 is True

        # Third request should fail
        allowed3, retry_after = limiter.check("client1")
        assert allowed3 is False
        assert retry_after > 0.0

    def test_default_burst_size(self):
        """Default burst size is 2x per-minute rate."""
        limiter = RateLimiter(requests_per_minute=60.0)
        # 60 per minute = 1 per second, capacity should be 2 (2x per second)
        assert limiter._capacity == 2.0

    def test_custom_burst_size(self):
        """Custom burst size is respected."""
        limiter = RateLimiter(requests_per_minute=60.0, burst_size=5.0)
        assert limiter._capacity == 5.0

    def test_cleanup_removes_stale_buckets(self):
        """Cleanup removes inactive client buckets."""
        limiter = RateLimiter(requests_per_minute=60.0)

        # Create a bucket
        limiter.check("stale_client")
        assert "stale_client" in limiter._buckets

        # Make the bucket stale
        old_time = time.time() - 600  # 10 minutes ago
        limiter._buckets["stale_client"]._last_update = old_time
        limiter._last_cleanup = old_time

        # Trigger cleanup
        limiter._cleanup_old_buckets()

        assert "stale_client" not in limiter._buckets


class TestRateLimitMiddleware:
    """Tests for Starlette rate limit middleware."""

    @pytest.fixture
    def app_with_middleware(self):
        """Create test app with rate limit middleware."""

        async def homepage(request):
            return Response("OK", media_type="text/plain")

        async def health(request):
            return Response("healthy", media_type="text/plain")

        app = Starlette(
            routes=[
                Route("/", homepage),
                Route("/health", health),
            ]
        )
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=60.0,
            exclude_paths=["/health"],
        )
        return app

    def test_allows_request_within_limit(self, app_with_middleware):
        """Middleware allows requests within limit."""
        client = TestClient(app_with_middleware)

        response = client.get("/")
        assert response.status_code == 200
        assert response.text == "OK"

    def test_excludes_health_endpoint(self, app_with_middleware):
        """Excluded paths bypass rate limiting."""
        client = TestClient(app_with_middleware)

        # Health endpoint should always work
        for _ in range(10):
            response = client.get("/health")
            assert response.status_code == 200

    def test_returns_429_when_rate_exceeded(self):
        """Returns 429 when rate limit exceeded."""

        async def homepage(request):
            return Response("OK", media_type="text/plain")

        app = Starlette(routes=[Route("/", homepage)])
        # Very low limit for testing
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=1.0,
            burst_size=1.0,
        )

        client = TestClient(app)

        # First request succeeds
        response1 = client.get("/")
        assert response1.status_code == 200

        # Second request should be rate limited
        response2 = client.get("/")
        assert response2.status_code == 429
        assert "Rate limit exceeded" in response2.json()["error"]
        assert "Retry-After" in response2.headers

    def test_uses_api_key_header_for_client_id(self):
        """Uses API key header for client identification."""

        async def homepage(request):
            return Response("OK", media_type="text/plain")

        app = Starlette(routes=[Route("/", homepage)])
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=1.0,
            burst_size=1.0,
            client_id_header="X-API-Key",
        )

        client = TestClient(app)

        # Request with key1
        response1 = client.get("/", headers={"X-API-Key": "key1"})
        assert response1.status_code == 200

        # Request with key2 (different client)
        response2 = client.get("/", headers={"X-API-Key": "key2"})
        assert response2.status_code == 200

        # Second request with key1 should be limited
        response3 = client.get("/", headers={"X-API-Key": "key1"})
        assert response3.status_code == 429


class TestCreateRateLimitMiddleware:
    """Tests for rate limit middleware factory."""

    def test_creates_configured_middleware(self):
        """Factory creates middleware with configuration."""
        middleware_class = create_rate_limit_middleware(
            requests_per_minute=120.0,
            burst_size=10.0,
            exclude_paths=["/custom/health"],
        )

        async def homepage(request):
            return Response("OK", media_type="text/plain")

        async def custom_health(request):
            return Response("healthy", media_type="text/plain")

        app = Starlette(
            routes=[
                Route("/", homepage),
                Route("/custom/health", custom_health),
            ]
        )
        app.add_middleware(middleware_class)

        client = TestClient(app)

        # Verify middleware is working by testing behavior
        response = client.get("/")
        assert response.status_code == 200

        # Custom health should be excluded
        response = client.get("/custom/health")
        assert response.status_code == 200

    def test_factory_preserves_configuration(self):
        """Factory middleware preserves configuration."""
        middleware_class = create_rate_limit_middleware(
            requests_per_minute=30.0,
            burst_size=5.0,
            exclude_paths=["/skip-me"],
        )

        async def homepage(request):
            return Response("OK", media_type="text/plain")

        async def skip_endpoint(request):
            return Response("Skipped", media_type="text/plain")

        app = Starlette(
            routes=[
                Route("/", homepage),
                Route("/skip-me", skip_endpoint),
            ]
        )
        app.add_middleware(middleware_class)

        client = TestClient(app)

        # Regular endpoint should be rate limited
        response1 = client.get("/")
        assert response1.status_code == 200

        # Excluded endpoint should never be limited
        for _ in range(20):
            response = client.get("/skip-me")
            assert response.status_code == 200
