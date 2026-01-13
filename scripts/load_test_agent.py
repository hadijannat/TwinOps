#!/usr/bin/env python3
"""Simple async load test for the agent chat endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import aiohttp


async def _run_one(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    headers: dict,
    sem: asyncio.Semaphore,
    latencies: list[float],
    errors: list[int],
) -> None:
    async with sem:
        start = time.perf_counter()
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                await resp.text()
                if resp.status >= 400:
                    errors.append(resp.status)
        except Exception:
            errors.append(599)
        finally:
            latencies.append(time.perf_counter() - start)


async def run_load_test(
    url: str,
    requests: int,
    concurrency: int,
    role: str,
    timeout: aiohttp.ClientTimeout,
) -> tuple[float, float, float, float, int]:
    payload = {"message": "Get status"}
    headers = {"Content-Type": "application/json", "X-Roles": role}

    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors: list[int] = []

    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [
            _run_one(session, url, payload, headers, sem, latencies, errors)
            for _ in range(requests)
        ]
        start = time.perf_counter()
        await asyncio.gather(*tasks)
        total = time.perf_counter() - start

    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[int(0.50 * len(latencies_sorted)) - 1]
    p95 = latencies_sorted[int(0.95 * len(latencies_sorted)) - 1]
    rps = requests / total if total > 0 else 0.0
    error_count = len(errors)
    return total, rps, p50, p95, error_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8080/chat")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--role", default="operator")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-error-rate", type=float, default=0.1)
    args = parser.parse_args()

    total, rps, p50, p95, error_count = asyncio.run(
        run_load_test(
            url=args.url,
            requests=args.requests,
            concurrency=args.concurrency,
            role=args.role,
            timeout=aiohttp.ClientTimeout(total=args.timeout),
        )
    )

    error_rate = error_count / args.requests if args.requests else 0.0
    summary = {
        "requests": args.requests,
        "concurrency": args.concurrency,
        "total_seconds": round(total, 3),
        "rps": round(rps, 2),
        "p50_ms": round(p50 * 1000, 2),
        "p95_ms": round(p95 * 1000, 2),
        "errors": error_count,
        "error_rate": round(error_rate, 3),
    }

    print(json.dumps(summary, indent=2))
    if error_rate > args.max_error_rate:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
