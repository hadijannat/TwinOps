"""Rate limiting middleware for API protection."""

import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from twinops.common.logging import get_logger

logger = get_logger(__name__)


class TokenBucket:
    """Token bucket rate limiter implementation."""

    def __init__(
        self,
        rate: float,
        capacity: float,
    ):
        """
        Initialize token bucket.

        Args:
            rate: Tokens added per second
            capacity: Maximum tokens in bucket
        """
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_update = time.time()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_update
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._rate,
        )
        self._last_update = now

    def consume(self, tokens: float = 1.0) -> bool:
        """
        Try to consume tokens from the bucket.

        Args:
            tokens: Number of tokens to consume

        Returns:
            True if tokens were consumed, False if insufficient
        """
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    @property
    def tokens_available(self) -> float:
        """Get current available tokens."""
        self._refill()
        return self._tokens

    def time_until_available(self, tokens: float = 1.0) -> float:
        """
        Calculate time until tokens are available.

        Args:
            tokens: Number of tokens needed

        Returns:
            Seconds until tokens are available
        """
        self._refill()
        if self._tokens >= tokens:
            return 0.0
        deficit = tokens - self._tokens
        return deficit / self._rate


class RateLimiter:
    """Per-client rate limiter with configurable limits."""

    def __init__(
        self,
        requests_per_minute: float = 60.0,
        burst_size: float | None = None,
    ):
        """
        Initialize rate limiter.

        Args:
            requests_per_minute: Sustained request rate per minute
            burst_size: Maximum burst size (defaults to 2x per-minute rate)
        """
        self._rate = requests_per_minute / 60.0  # Convert to per-second
        self._capacity = burst_size or (requests_per_minute * 2 / 60.0)
        self._buckets: dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(self._rate, self._capacity)
        )
        self._cleanup_interval = 300.0  # 5 minutes
        self._last_cleanup = time.time()

    def _cleanup_old_buckets(self) -> None:
        """Remove inactive client buckets to prevent memory growth."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        # Remove buckets that haven't been used recently
        stale_clients = [
            client_id
            for client_id, bucket in self._buckets.items()
            if now - bucket._last_update > self._cleanup_interval
        ]
        for client_id in stale_clients:
            del self._buckets[client_id]

        self._last_cleanup = now
        if stale_clients:
            logger.debug("Cleaned up rate limit buckets", count=len(stale_clients))

    def check(self, client_id: str) -> tuple[bool, float]:
        """
        Check if request is allowed for client.

        Args:
            client_id: Client identifier

        Returns:
            Tuple of (allowed, retry_after_seconds)
        """
        self._cleanup_old_buckets()
        bucket = self._buckets[client_id]

        if bucket.consume():
            return True, 0.0
        else:
            retry_after = bucket.time_until_available()
            return False, retry_after


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette middleware for rate limiting requests."""

    def __init__(
        self,
        app: ASGIApp,
        requests_per_minute: float = 60.0,
        burst_size: float | None = None,
        exclude_paths: list[str] | None = None,
        client_id_header: str = "X-API-Key",
    ) -> None:
        """
        Initialize rate limit middleware.

        Args:
            app: Starlette application
            requests_per_minute: Sustained request rate per minute
            burst_size: Maximum burst size
            exclude_paths: Paths to exclude from rate limiting
            client_id_header: Header to use for client identification
        """
        super().__init__(app)
        self._limiter = RateLimiter(
            requests_per_minute=requests_per_minute,
            burst_size=burst_size,
        )
        self._exclude_paths = set(exclude_paths or ["/health", "/ready", "/metrics"])
        self._client_id_header = client_id_header

    def _get_client_id(self, request: Request) -> str:
        """Extract client identifier from request."""
        # Try API key header first
        api_key = request.headers.get(self._client_id_header)
        if api_key:
            return f"key:{api_key}"

        # Fall back to client IP
        client_ip = request.client.host if request.client else "unknown"
        return f"ip:{client_ip}"

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Process request with rate limiting."""
        # Skip rate limiting for excluded paths
        if request.url.path in self._exclude_paths:
            return await call_next(request)

        client_id = self._get_client_id(request)
        allowed, retry_after = self._limiter.check(client_id)

        if not allowed:
            retry_after_int = max(1, int(retry_after) + 1)
            logger.warning(
                "Rate limit exceeded",
                client_id=client_id,
                path=request.url.path,
                retry_after=retry_after_int,
            )
            return JSONResponse(
                {
                    "error": "Rate limit exceeded",
                    "retry_after": retry_after_int,
                },
                status_code=429,
                headers={"Retry-After": str(retry_after_int)},
            )

        return await call_next(request)


def create_rate_limit_middleware(
    requests_per_minute: float = 60.0,
    burst_size: float | None = None,
    exclude_paths: list[str] | None = None,
) -> type[RateLimitMiddleware]:
    """
    Factory function to create rate limit middleware with configuration.

    Args:
        requests_per_minute: Sustained request rate per minute
        burst_size: Maximum burst size
        exclude_paths: Paths to exclude from rate limiting

    Returns:
        Configured middleware class
    """
    class ConfiguredRateLimitMiddleware(RateLimitMiddleware):
        def __init__(self, app: ASGIApp) -> None:
            super().__init__(
                app,
                requests_per_minute=requests_per_minute,
                burst_size=burst_size,
                exclude_paths=exclude_paths,
            )

    return ConfiguredRateLimitMiddleware
