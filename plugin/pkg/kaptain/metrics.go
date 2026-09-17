package kaptain

import (
	"sync"

	"k8s.io/component-base/metrics"
	"k8s.io/component-base/metrics/legacyregistry"
)

// Metrics are registered on the scheduler's own /metrics endpoint. Labels stay low
// cardinality on purpose: no pod UID, no node name, no image. Those go to the decision
// log instead.
var (
	scoreDuration = metrics.NewHistogramVec(&metrics.HistogramOpts{
		Subsystem:      subsystem,
		Name:           "score_duration_seconds",
		Help:           "Time spent producing the scores for one pod, fallback included.",
		Buckets:        metrics.ExponentialBuckets(0.0001, 2, 14),
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy", "integration", "status"})

	snapshotDuration = metrics.NewHistogramVec(&metrics.HistogramOpts{
		Subsystem:      subsystem,
		Name:           "snapshot_duration_seconds",
		Help:           "Time spent building the decision snapshot.",
		Buckets:        metrics.ExponentialBuckets(0.00001, 2, 14),
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy", "status"})

	deciderDuration = metrics.NewHistogramVec(&metrics.HistogramOpts{
		Subsystem:      subsystem,
		Name:           "decider_duration_seconds",
		Help:           "Time spent in the local strategy or in the external decider call.",
		Buckets:        metrics.ExponentialBuckets(0.0001, 2, 14),
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy", "decider", "status"})

	normalizeDuration = metrics.NewHistogramVec(&metrics.HistogramOpts{
		Subsystem:      subsystem,
		Name:           "normalize_duration_seconds",
		Help:           "Time spent normalising raw scores into the Kubernetes scale.",
		Buckets:        metrics.ExponentialBuckets(0.00001, 2, 12),
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy"})

	candidateNodes = metrics.NewHistogramVec(&metrics.HistogramOpts{
		Subsystem:      subsystem,
		Name:           "candidate_nodes",
		Help:           "Number of candidate nodes seen per decision.",
		Buckets:        []float64{1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000},
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy"})

	decisionsTotal = metrics.NewCounterVec(&metrics.CounterOpts{
		Subsystem:      subsystem,
		Name:           "decisions_total",
		Help:           "Decisions attempted, by outcome.",
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy", "status"})

	fallbackTotal = metrics.NewCounterVec(&metrics.CounterOpts{
		Subsystem:      subsystem,
		Name:           "fallback_total",
		Help:           "Decisions where the explicit fallback was used, by reason.",
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy", "reason"})

	invalidDecisionTotal = metrics.NewCounterVec(&metrics.CounterOpts{
		Subsystem:      subsystem,
		Name:           "invalid_decision_total",
		Help:           "Invalid decisions: unknown node, invalid score, missing snapshot.",
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy", "reason"})

	cacheAgeSeconds = metrics.NewHistogramVec(&metrics.HistogramOpts{
		Subsystem:      subsystem,
		Name:           "cache_age_seconds",
		Help:           "Age of the measurements used in a snapshot, by source.",
		Buckets:        []float64{0.1, 0.5, 1, 2, 5, 10, 30, 60, 300},
		StabilityLevel: metrics.ALPHA,
	}, []string{"source"})

	boundMismatchTotal = metrics.NewCounterVec(&metrics.CounterOpts{
		Subsystem:      subsystem,
		Name:           "bound_mismatch_total",
		Help:           "Pods bound to a node other than the intended one, which happens when normalised scores tie.",
		StabilityLevel: metrics.ALPHA,
	}, []string{"strategy"})

	missingFeaturesTotal = metrics.NewCounterVec(&metrics.CounterOpts{
		Subsystem:      subsystem,
		Name:           "missing_features_total",
		Help:           "Features absent from a snapshot, by feature group.",
		StabilityLevel: metrics.ALPHA,
	}, []string{"feature_group"})
)

const subsystem = "kaptain_plugin"

var registerOnce sync.Once

func registerMetrics() {
	registerOnce.Do(func() {
		legacyregistry.MustRegister(
			scoreDuration,
			snapshotDuration,
			deciderDuration,
			normalizeDuration,
			candidateNodes,
			decisionsTotal,
			fallbackTotal,
			invalidDecisionTotal,
			cacheAgeSeconds,
			boundMismatchTotal,
			missingFeaturesTotal,
		)
	})
}
