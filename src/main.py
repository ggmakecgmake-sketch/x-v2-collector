"""
X-v2 Collector — standalone service entry point.

Runs two asyncio tasks concurrently:
1. FastAPI server on port 8001 for health/metrics
2. The scraping loop (twikit → fallback playwright)

Publishes tweets to Redis Stream 'tweets:raw' with schema compatible
with the existing v1 collector.
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import structlog
import uvicorn
from fastapi import FastAPI

from src.config import settings
from src.core.deduplicator import Deduplicator
from src.core.rate_limiter import RateLimiter
from src.core.redis_publisher import RedisPublisher
from src.engine.twikit_engine import TwikitEngine
from src.engine.playwright_engine import PlaywrightEngine
from src.api.health import router as health_router, update_state

# Configure structural logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.log_level)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
logger = structlog.get_logger("x_v2_collector")


# ============================================================================
# FastAPI app
# ============================================================================
app = FastAPI(title=settings.app_name, version="0.1.0")
app.include_router(health_router, prefix="", tags=["health"])


# ============================================================================
# Collector loop
# ============================================================================
class CollectorApp:
    def __init__(self):
        self.running = False
        self.publisher = RedisPublisher()
        self.deduplicator = Deduplicator(max_per_account=200)
        self.rate_limiter = RateLimiter(
            min_seconds=settings.poll_interval_min,
            max_seconds=settings.poll_interval_max,
        )

        # Engines
        self.engine: TwikitEngine | PlaywrightEngine | None = None
        self.engine_name = "twikit"
        self.failed_engines: set = set()

    async def run(self):
        logger.info("collector_starting", accounts=settings.x_accounts_to_track)
        self.running = True

        # Connect to Redis
        try:
            await self.publisher.connect()
            update_state("healthy", True)
        except Exception as exc:
            logger.error("redis_connection_failed", error=str(exc))
            update_state("healthy", False)
            return

        # Initialize primary engine
        if not await self._start_engine("twikit"):
            update_state("healthy", False)
            return

        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        # Main loop
        try:
            await self._collect_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self):
        if not self.running:
            return
        self.running = False
        logger.info("collector_shutting_down")
        update_state("healthy", False)
        if self.engine:
            try:
                await self.engine.stop()
            except Exception:
                pass
        await self.publisher.disconnect()
        logger.info("collector_shutdown_complete")

    # ------------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------------

    async def _start_engine(self, name: str) -> bool:
        logger.info("starting_engine", name=name)
        try:
            if name == "twikit":
                self.engine = TwikitEngine()
            elif name == "playwright":
                self.engine = PlaywrightEngine(headless=True)
            else:
                logger.error("unknown_engine", name=name)
                return False

            await self.engine.start()
            self.engine_name = name
            self.failed_engines.discard(name)
            update_state("current_engine", name)
            logger.info("engine_started", name=name)
            return True

        except Exception as exc:
            logger.error("engine_start_failed", name=name, error=str(exc))
            self.failed_engines.add(name)
            self.engine = None
            return False

    async def _switch_engine(self, to: str) -> bool:
        if to in self.failed_engines:
            logger.error("engine_already_failed", name=to)
            return False
        if self.engine:
            try:
                await self.engine.stop()
            except Exception:
                pass
        return await self._start_engine(to)

    # ------------------------------------------------------------------
    # Collection loop
    # ------------------------------------------------------------------

    async def _collect_loop(self):
        while self.running:
            for account in settings.x_accounts_to_track:
                if not self.running:
                    break

                await self.rate_limiter.wait_for(account)

                try:
                    if self.engine is None:
                        raise RuntimeError("No engine available")

                    tweets = await self.engine.fetch_timeline(account)
                    self.rate_limiter.mark_fetched(account)
                    update_state("last_fetch", datetime.now(timezone.utc).isoformat())

                    new_count = 0
                    for tweet in tweets:
                        if self.deduplicator.is_new(account, tweet.tweet_id):
                            await self.publisher.publish(tweet)
                            self.deduplicator.add(account, tweet.tweet_id)
                            new_count += 1

                    update_state("tweets_total", _state_val("tweets_total", 0) + new_count)
                    update_state("tweets_this_hour", _state_val("tweets_this_hour", 0) + new_count)
                    update_state("healthy", True)
                    logger.info(
                        "collection_cycle_ok",
                        account=account,
                        fetched=len(tweets),
                        new=new_count,
                        engine=self.engine_name,
                    )

                except Exception as exc:
                    update_state("errors_total", _state_val("errors_total", 0) + 1)
                    update_state("healthy", False)
                    logger.error(
                        "collection_cycle_error",
                        account=account,
                        error=str(exc),
                        engine=self.engine_name,
                        failures=getattr(self.engine, "failure_count", 0),
                    )

                    # Decide if fallback is needed
                    if self.engine_name == "twikit":
                        failures = getattr(self.engine, "failure_count", 0)
                        if failures >= settings.twikit_failure_threshold:
                            logger.warning("twikit_threshold_reached_switching_to_playwright")
                            if await self._switch_engine("playwright"):
                                update_state("current_engine", "playwright")
                            else:
                                logger.critical("no_engine_available")
                                self.running = False
                                break

                    # If playwright fails too, back off
                    elif self.engine_name == "playwright":
                        logger.warning("playwright_error_backing_off_5min")
                        await asyncio.sleep(300)

    # ------------------------------------------------------------------
    # Task runner (FastAPI + collector loop)
    # ------------------------------------------------------------------

    async def run_server(self):
        """Run both the FastAPI server and collector concurrently."""
        server = uvicorn.Server(
            uvicorn.Config(
                app=app,
                host=settings.health_host,
                port=settings.health_port,
                log_level=settings.log_level.lower(),
            )
        )
        collector_task = asyncio.create_task(self.run())
        server_task = asyncio.create_task(server.serve())

        try:
            await asyncio.gather(collector_task, server_task)
        except asyncio.CancelledError:
            pass
        finally:
            collector_task.cancel()
            server_task.cancel()
            try:
                await collector_task
            except asyncio.CancelledError:
                pass
            try:
                await server_task
            except asyncio.CancelledError:
                pass


# ============================================================================
# Utilities
# ============================================================================

def _state_val(key: str, default: int) -> int:
    from src.api.health import _state
    return _state.get(key, default)


# ============================================================================
# Entry point
# ============================================================================

def main():
    app_runner = CollectorApp()
    try:
        asyncio.run(app_runner.run_server())
    except KeyboardInterrupt:
        logger.info("interrupted_by_user")
    finally:
        logger.info("collector_exited")


if __name__ == "__main__":
    main()
