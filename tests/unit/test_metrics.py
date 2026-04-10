"""Unit tests for Prometheus metrics — correct types, names, and labels."""

from prometheus_client import Counter, Gauge, Histogram

from app.metrics import (
    agent_invocation_duration_seconds,
    events_received_total,
    items_failed,
    items_in_flight,
    items_ready,
    items_released_stale_total,
    items_waiting_manual,
    phases_completed_total,
    phases_failed_total,
    reconcile_cycle_duration_seconds,
    reviews_total,
)


class TestMetricTypes:
    def test_counters(self):
        for metric in (
            events_received_total,
            reviews_total,
            phases_completed_total,
            phases_failed_total,
            items_released_stale_total,
        ):
            assert isinstance(metric, Counter), f"{metric} is not a Counter"

    def test_gauges(self):
        for metric in (
            items_waiting_manual,
            items_in_flight,
            items_ready,
            items_failed,
        ):
            assert isinstance(metric, Gauge), f"{metric} is not a Gauge"

    def test_histograms(self):
        for metric in (
            agent_invocation_duration_seconds,
            reconcile_cycle_duration_seconds,
        ):
            assert isinstance(metric, Histogram), f"{metric} is not a Histogram"


class TestMetricNames:
    def test_all_prefixed(self):
        metrics = [
            events_received_total,
            reviews_total,
            phases_completed_total,
            phases_failed_total,
            items_waiting_manual,
            items_in_flight,
            items_ready,
            items_failed,
            agent_invocation_duration_seconds,
            reconcile_cycle_duration_seconds,
            items_released_stale_total,
        ]
        for metric in metrics:
            desc = metric.describe()[0]
            assert desc.name.startswith("ols_automator_"), f"{desc.name} missing prefix"


class TestMetricLabels:
    def test_events_received_labels(self):
        assert events_received_total._labelnames == ("event_type", "status")

    def test_reviews_labels(self):
        assert reviews_total._labelnames == ("command",)

    def test_phases_completed_labels(self):
        assert phases_completed_total._labelnames == ("policy", "phase")

    def test_phases_failed_labels(self):
        assert phases_failed_total._labelnames == ("policy", "phase")

    def test_agent_duration_labels(self):
        assert agent_invocation_duration_seconds._labelnames == ("agent",)

    def test_reconcile_duration_no_labels(self):
        assert reconcile_cycle_duration_seconds._labelnames == ()

    def test_gauges_no_labels(self):
        for metric in (
            items_waiting_manual,
            items_in_flight,
            items_ready,
            items_failed,
        ):
            assert metric._labelnames == (), f"{metric} has unexpected labels"

    def test_stale_released_no_labels(self):
        assert items_released_stale_total._labelnames == ()
