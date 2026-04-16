"""Prometheus metrics for OLS Automator."""

from prometheus_client import Counter, Gauge, Histogram

events_received_total = Counter(
    "ols_automator_events_received_total",
    "Total events received by the ingestion endpoint",
    ["event_type", "status"],
)

reviews_total = Counter(
    "ols_automator_reviews_total",
    "Total manual review actions",
    ["command"],
)

failed_item_actions_total = Counter(
    "ols_automator_failed_item_actions_total",
    "Actions on failed work items (delete or retry)",
    ["command"],
)

phases_completed_total = Counter(
    "ols_automator_phases_completed_total",
    "Total successful phase transitions",
    ["policy", "phase"],
)

phases_failed_total = Counter(
    "ols_automator_phases_failed_total",
    "Total phase failures",
    ["policy", "phase"],
)

items_waiting_manual = Gauge(
    "ols_automator_items_waiting_manual",
    "Work items waiting for manual approval",
)

items_in_flight = Gauge(
    "ols_automator_items_in_flight",
    "Work items currently being processed by agents",
)

items_ready = Gauge(
    "ols_automator_items_ready",
    "Work items ready for processing",
)

items_failed = Gauge(
    "ols_automator_items_failed",
    "Work items in failed state",
)

agent_invocation_duration_seconds = Histogram(
    "ols_automator_agent_invocation_duration_seconds",
    "Time spent invoking A2A agents",
    ["agent"],
)

reconcile_cycle_duration_seconds = Histogram(
    "ols_automator_reconcile_cycle_duration_seconds",
    "Duration of each reconciliation loop iteration",
)

items_released_stale_total = Counter(
    "ols_automator_items_released_stale_total",
    "Work items re-queued after exceeding the stale lock timeout",
)
