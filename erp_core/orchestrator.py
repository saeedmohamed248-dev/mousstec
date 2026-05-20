"""
Mouss Tec — Central Multi-Agent Orchestration Engine
=====================================================
Provides the shared infrastructure for all agents (bots) in the pipeline:

  CircuitBreaker    — isolates a failing agent so the chain doesn't collapse
  DeadLetterQueue   — persists failed tasks for later retry / inspection
  AgentEventBus     — lightweight state-sync bus (Redis-backed)
  AgentHealthMonitor— real-time health snapshots per agent
  AgentRegistry     — authoritative list of every agent in the system

All primitives are stateless classes backed by Redis so they work correctly
in multi-process / multi-worker Celery deployments.
"""
import json
import time
import functools
import logging
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

from django.core.cache import cache

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 1. Circuit Breaker
# ─────────────────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED    = "closed"     # normal – requests pass through
    OPEN      = "open"       # agent isolated – requests short-circuit
    HALF_OPEN = "half_open"  # one probe request allowed to test recovery


class CircuitOpenError(Exception):
    """Raised when a decorated function is called while its circuit is OPEN."""


class CircuitBreaker:
    """
    Per-agent distributed circuit breaker.

    State transitions:
        CLOSED  → OPEN      after `failure_threshold` consecutive failures
        OPEN    → HALF_OPEN after `recovery_timeout` seconds
        HALF_OPEN → CLOSED  on first success
        HALF_OPEN → OPEN    on first failure (re-opens immediately)

    All state is stored in Redis so every Celery worker shares the same view.

    Usage as a decorator:
        breaker = AgentRegistry.get_circuit_breaker('my_agent')

        @breaker
        def my_agent_logic(...):
            ...

    Or inline:
        breaker = CircuitBreaker('my_agent')
        try:
            result = breaker.call(my_function, arg1, arg2)
        except CircuitOpenError:
            # handle graceful degradation
    """

    def __init__(self, agent_name: str, failure_threshold: int = 3, recovery_timeout: int = 60):
        self.agent_name       = agent_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._state_key        = f"cb_state:{agent_name}"
        self._failures_key     = f"cb_failures:{agent_name}"
        self._last_fail_key    = f"cb_last_fail:{agent_name}"

    # ------------------------------------------------------------------
    @property
    def state(self) -> CircuitState:
        raw = cache.get(self._state_key, CircuitState.CLOSED.value)
        if raw == CircuitState.OPEN.value:
            last_fail = cache.get(self._last_fail_key, 0)
            if time.time() - float(last_fail) >= self.recovery_timeout:
                cache.set(self._state_key, CircuitState.HALF_OPEN.value, timeout=None)
                return CircuitState.HALF_OPEN
            return CircuitState.OPEN
        try:
            return CircuitState(raw)
        except ValueError:
            return CircuitState.CLOSED

    def is_available(self) -> bool:
        return self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self):
        cache.set(self._state_key, CircuitState.CLOSED.value, timeout=None)
        cache.set(self._failures_key, 0, timeout=None)
        logger.info(f"✅ [CIRCUIT BREAKER] Agent '{self.agent_name}' → CLOSED (recovered)")

    def record_failure(self):
        failures = (cache.get(self._failures_key) or 0) + 1
        cache.set(self._failures_key, failures, timeout=3600)
        cache.set(self._last_fail_key, time.time(), timeout=3600)

        if failures >= self.failure_threshold:
            cache.set(self._state_key, CircuitState.OPEN.value, timeout=None)
            logger.critical(
                f"🔴 [CIRCUIT BREAKER] Agent '{self.agent_name}' → OPEN "
                f"after {failures} failures. Isolated for {self.recovery_timeout}s."
            )
        else:
            logger.warning(
                f"⚠️ [CIRCUIT BREAKER] Agent '{self.agent_name}' "
                f"failure {failures}/{self.failure_threshold}"
            )

    # ------------------------------------------------------------------
    def call(self, func: Callable, *args, **kwargs):
        """Inline usage: result = breaker.call(fn, arg1, arg2)"""
        if not self.is_available():
            raise CircuitOpenError(f"Agent '{self.agent_name}' circuit is OPEN.")
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except CircuitOpenError:
            raise
        except Exception:
            self.record_failure()
            raise

    def __call__(self, func: Callable) -> Callable:
        """Decorator usage: @breaker"""
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)
        return wrapper


# ─────────────────────────────────────────────────────────────────────
# 2. Dead Letter Queue
# ─────────────────────────────────────────────────────────────────────

class DeadLetterQueue:
    """
    Bounded Redis list that stores failed agent payloads.

    When a Celery task or signal handler fails after all retries, it should
    push to the DLQ so the failure is preserved for debugging / manual replay.

    Max size: 1000 entries (oldest are evicted automatically via LTRIM).
    """

    MAX_SIZE = 1000

    def __init__(self, queue_name: str = "mas_dlq"):
        self._key = f"dlq:{queue_name}"

    # ------------------------------------------------------------------
    def push(self, agent_name: str, payload: dict, error: str, schema: str = "public"):
        entry = json.dumps({
            "agent":       agent_name,
            "schema":      schema,
            "payload":     payload,
            "error":       str(error)[:800],
            "timestamp":   datetime.utcnow().isoformat(),
            "retry_count": 0,
        })
        try:
            raw_client = cache.client.get_client()
            raw_client.lpush(self._key, entry)
            raw_client.ltrim(self._key, 0, self.MAX_SIZE - 1)
            logger.error(
                f"📬 [DLQ] Task from agent '{agent_name}' (schema: {schema}) "
                f"moved to Dead Letter Queue. Error: {str(error)[:120]}"
            )
        except Exception as exc:
            # DLQ must never crash the caller
            logger.error(f"🔴 [DLQ ERROR] Could not persist failed task: {exc}")

    def peek(self, count: int = 20) -> list:
        try:
            raw_client = cache.client.get_client()
            entries = raw_client.lrange(self._key, 0, count - 1)
            return [json.loads(e) for e in entries]
        except Exception as exc:
            logger.error(f"🔴 [DLQ ERROR] Could not read DLQ: {exc}")
            return []

    def size(self) -> int:
        try:
            return cache.client.get_client().llen(self._key)
        except Exception:
            return -1

    def pop(self) -> Optional[dict]:
        """Remove and return the oldest entry (FIFO replay)."""
        try:
            raw_client = cache.client.get_client()
            raw = raw_client.rpop(self._key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.error(f"🔴 [DLQ ERROR] Could not pop from DLQ: {exc}")
            return None


# Singleton DLQ used by all agents
dlq = DeadLetterQueue()


# ─────────────────────────────────────────────────────────────────────
# 3. Agent Event Bus  (state synchronisation across agents)
# ─────────────────────────────────────────────────────────────────────

class AgentEventBus:
    """
    Lightweight state-sync bus backed by Redis.

    Two complementary APIs:
    ─────────────────────────────────────────────────────────────────
    A) State snapshots  (poll-based)
       AgentEventBus.set_agent_state('outbound_orchestrator', {...})
       state = AgentEventBus.get_agent_state('outbound_orchestrator')

    B) Event publishing  (fire-and-forget, Redis PUBLISH)
       AgentEventBus.publish('low_stock_detected', {'product_id': 42}, schema='acme')

    Agents that need to *react* to events in real-time should use
    Django Channels consumers (ws) or Celery tasks as subscribers.
    ─────────────────────────────────────────────────────────────────
    """

    # ------------------------------------------------------------------
    @staticmethod
    def set_agent_state(agent_name: str, state: dict,
                        schema: str = "public", ttl: int = 3600):
        """Persist agent output state so downstream agents can read it."""
        key = f"mas_state:{schema}:{agent_name}"
        try:
            state_with_meta = {
                **state,
                "_updated_at": datetime.utcnow().isoformat(),
                "_agent": agent_name,
                "_schema": schema,
            }
            cache.set(key, state_with_meta, timeout=ttl)
        except Exception as exc:
            logger.error(f"🔴 [EVENT BUS] set_agent_state failed for '{agent_name}': {exc}")

    @staticmethod
    def get_agent_state(agent_name: str, schema: str = "public") -> Optional[dict]:
        """Read the latest state snapshot published by an agent."""
        key = f"mas_state:{schema}:{agent_name}"
        return cache.get(key)

    # ------------------------------------------------------------------
    @staticmethod
    def publish(event_type: str, data: dict, schema: str = "public"):
        """
        Fire a Redis PUBLISH message on channel  mas_events:<schema>:<event_type>

        Celery tasks or Channels consumers can subscribe to these channels.
        The call is intentionally non-blocking — any publish failure is logged
        but does NOT raise, so the calling agent's transaction is unaffected.
        """
        try:
            payload = json.dumps({
                "event_type": event_type,
                "schema":     schema,
                "data":       data,
                "timestamp":  datetime.utcnow().isoformat(),
            })
            channel = f"mas_events:{schema}:{event_type}"
            cache.client.get_client().publish(channel, payload)
            logger.debug(f"📡 [EVENT BUS] Published '{event_type}' on schema '{schema}'")
        except Exception as exc:
            logger.error(f"🔴 [EVENT BUS] publish failed for '{event_type}': {exc}")

    # ------------------------------------------------------------------
    @staticmethod
    def push_pipeline_event(event_type: str, data: dict, schema: str = "public"):
        """
        Durable alternative to publish() — writes to a bounded Redis list
        so events are not lost if no subscriber is connected at publish time.

        Celery Beat task `drain_pipeline_events` processes this list.
        """
        key = f"mas_pipeline_events:{schema}"
        entry = json.dumps({
            "event_type": event_type,
            "schema":     schema,
            "data":       data,
            "timestamp":  datetime.utcnow().isoformat(),
        })
        try:
            raw_client = cache.client.get_client()
            raw_client.lpush(key, entry)
            raw_client.ltrim(key, 0, 4999)  # keep last 5000 events
        except Exception as exc:
            logger.error(f"🔴 [EVENT BUS] push_pipeline_event failed: {exc}")


# ─────────────────────────────────────────────────────────────────────
# 4. Agent Health Monitor
# ─────────────────────────────────────────────────────────────────────

class AgentHealthMonitor:
    """
    Tracks liveness of every agent via lightweight Redis heartbeats.

    Agents call AgentHealthMonitor.heartbeat() at the end of each
    successful execution. The /system/health/ endpoint calls
    get_all_agents_health() to surface the report.
    """

    HEARTBEAT_TTL = 300  # 5 minutes — agent considered stale after this

    @staticmethod
    def heartbeat(agent_name: str, schema: str = "public", metadata: dict = None):
        key = f"mas_health:{schema}:{agent_name}"
        payload = {
            "status":    "alive",
            "last_seen": datetime.utcnow().isoformat(),
            "schema":    schema,
        }
        if metadata:
            payload.update(metadata)
        cache.set(key, payload, timeout=AgentHealthMonitor.HEARTBEAT_TTL)

    @staticmethod
    def mark_failed(agent_name: str, schema: str = "public", error: str = ""):
        key = f"mas_health:{schema}:{agent_name}"
        payload = {
            "status":     "failed",
            "last_error": str(error)[:300],
            "last_seen":  datetime.utcnow().isoformat(),
            "schema":     schema,
        }
        cache.set(key, payload, timeout=AgentHealthMonitor.HEARTBEAT_TTL)

    @staticmethod
    def get_all_agents_health(schema: str = "public") -> dict:
        report = {}
        for agent_name in AgentRegistry.REGISTERED_AGENTS:
            key = f"mas_health:{schema}:{agent_name}"
            health = cache.get(key)
            report[agent_name] = health or {"status": "unknown", "last_seen": None}
        return report

    @staticmethod
    def get_summary(schema: str = "public") -> dict:
        health = AgentHealthMonitor.get_all_agents_health(schema)
        alive   = sum(1 for v in health.values() if v.get("status") == "alive")
        failed  = sum(1 for v in health.values() if v.get("status") == "failed")
        unknown = sum(1 for v in health.values() if v.get("status") == "unknown")
        return {
            "total": len(health),
            "alive": alive,
            "failed": failed,
            "unknown": unknown,
            "details": health,
        }


# ─────────────────────────────────────────────────────────────────────
# 5. Agent Registry
# ─────────────────────────────────────────────────────────────────────

class AgentRegistry:
    """
    Authoritative catalogue of every agent (bot) in the Mouss Tec MAS.

    Also acts as a factory for per-agent CircuitBreaker instances so the
    same breaker object is reused across calls within a single process.
    """

    # All registered agents — add here when a new bot is introduced
    REGISTERED_AGENTS = [
        # ── Sync agents (Django signals / DB triggers) ──────────────
        "dynamic_calculator_agent",
        "reverse_logistics_agent",
        "inbound_orchestrator",
        "outbound_orchestrator",
        "logistics_agent",
        "b2b_sync_agent",
        "provisioning_orchestrator",
        # ── Async agents (Celery tasks) ──────────────────────────────
        "welcome_bot",
        "dunning_system",
        "ai_trust_score_engine",
        "ai_bidding_award_agent",
        "b2b_marketplace_sync_task",
        "b2b_marketplace_remove_task",
        "inventory_tasks_ai_vision",
        "inventory_tasks_financial",
        "maintenance_reminder_bot",
        # ── AI Cognitive agents ──────────────────────────────────────
        "diagnostic_bot",
        "vision_procurement_bot",
        "prognostic_maintenance_bot",
        "crm_churn_bot",
        "elastic_pricing_bot",
        # ── Infrastructure ───────────────────────────────────────────
        "ecosystem_watchdog",
        "dlq_retry_worker",
    ]

    _breakers: dict = {}  # process-local cache of CircuitBreaker objects

    @classmethod
    def get_circuit_breaker(
        cls,
        agent_name: str,
        failure_threshold: int = 3,
        recovery_timeout: int = 60,
    ) -> CircuitBreaker:
        """Return (or create) the CircuitBreaker for the given agent."""
        if agent_name not in cls._breakers:
            cls._breakers[agent_name] = CircuitBreaker(
                agent_name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return cls._breakers[agent_name]

    @classmethod
    def list_open_circuits(cls) -> list:
        """Return names of all agents whose circuit is currently OPEN."""
        return [
            name for name, breaker in cls._breakers.items()
            if breaker.state == CircuitState.OPEN
        ]


# ─────────────────────────────────────────────────────────────────────
# 6. Safe Agent Execution helper
# ─────────────────────────────────────────────────────────────────────

def run_agent_safely(
    agent_name: str,
    func: Callable,
    payload: dict = None,
    schema: str = "public",
    failure_threshold: int = 3,
    recovery_timeout: int = 60,
    reraise: bool = False,
):
    """
    Execute `func` with full MAS protection:
        1. Circuit breaker check (skip if OPEN)
        2. Execute function
        3. On success: record heartbeat + publish state
        4. On failure: record failure + push to DLQ
        5. Optionally reraise (for transactional contexts)

    Example usage inside a Celery task:
        run_agent_safely(
            agent_name='b2b_marketplace_sync_task',
            func=lambda: _do_sync(schema, product_id),
            payload={'schema': schema, 'product_id': product_id},
            schema=schema,
        )
    """
    payload = payload or {}
    breaker = AgentRegistry.get_circuit_breaker(
        agent_name,
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
    )

    if not breaker.is_available():
        logger.warning(
            f"⚡ [MAS] Agent '{agent_name}' circuit is OPEN — execution skipped."
        )
        return None

    try:
        result = func()
        breaker.record_success()
        AgentHealthMonitor.heartbeat(agent_name, schema=schema)
        logger.info(f"✅ [MAS] Agent '{agent_name}' completed successfully.")
        return result

    except CircuitOpenError:
        # Already logged by the breaker — just swallow
        return None

    except Exception as exc:
        breaker.record_failure()
        AgentHealthMonitor.mark_failed(agent_name, schema=schema, error=str(exc))
        dlq.push(agent_name, payload, error=str(exc), schema=schema)
        logger.error(f"🔴 [MAS] Agent '{agent_name}' failed: {exc}")
        if reraise:
            raise
        return None
