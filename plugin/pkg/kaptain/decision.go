package kaptain

import (
	"encoding/json"
	"math"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
)

// Integration labels every result line and every metric sample, so extender and plugin
// runs can be separated when the numbers are analysed.
const Integration = "plugin"

// maxRawScore bounds what a strategy or decider may return. It keeps the int64 conversion
// in Score far from overflow and rejects absurd values instead of ranking on them.
const maxRawScore = 1e9

// Fallback reasons. They are values in logs and in the fallback_total metric, so they stay
// a closed, low-cardinality set.
const (
	reasonStrategyError = "strategy_error"
	reasonDeciderError  = "decider_error"
	reasonInvalidChoice = "invalid_choice"
	reasonInvalidScore  = "invalid_score"
	reasonNoCandidates  = "no_candidates"
)

// Decision is what PreScore computes once per pod and Score then reads per node.
type Decision struct {
	Scores   map[string]float64
	Intended string
	Strategy string
	Decider  string

	FallbackReason string
	CandidateCount int
	TieCount       int
	RequestsAgeMs  float64
	TelemetryAgeMs float64

	Started        time.Time
	SnapshotMillis float64
	DeciderMillis  float64
	scoreNanos     atomic.Int64
}

func (d *Decision) Fallback() bool { return d.FallbackReason != "" }

func (d *Decision) status() string {
	if d.Fallback() {
		return "fallback"
	}
	return "ok"
}

func (d *Decision) addScoreTime(elapsed time.Duration) {
	d.scoreNanos.Add(int64(elapsed))
}

func (d *Decision) scoreMillis() float64 {
	return float64(d.scoreNanos.Load()) / float64(time.Millisecond)
}

var logMutex sync.Mutex

// logDecision writes one line per decision, at the end of normalisation so the reported
// duration covers the whole decision: snapshot, policy, per-node scoring and normalisation.
//
// intended_node is what our policy chose. It is not necessarily where the pod lands: when
// several nodes end up with the same normalised score, kube-scheduler picks among them at
// random. tie_count says how many shared the top score, and PostBind logs the node the
// scheduler actually bound.
func (p *Plugin) logDecision(s *snapshot.Snapshot, d *Decision, normalizeMillis, totalMillis float64) {
	line := map[string]any{
		"run_id":          s.RunID,
		"integration":     Integration,
		"strategy":        d.Strategy,
		"policy_version":  s.PolicyVersion,
		"pod_uid":         s.Pod.UID,
		"namespace":       s.Pod.Namespace,
		"pod_name":        s.Pod.Name,
		"candidate_count": d.CandidateCount,
		// Two ages, never one: they come from different sources at different rates.
		"requests_age_ms":  round(d.RequestsAgeMs),
		"telemetry_age_ms": round(d.TelemetryAgeMs),
		"intended_node":    d.Intended,
		"tie_count":        d.TieCount,
		"score":            round(d.Scores[d.Intended]),
		"fallback":         d.Fallback(),
		"fallback_reason":  nullable(d.FallbackReason),
		"fallback_strategy": func() any {
			if d.Fallback() {
				return FallbackName
			}
			return nil
		}(),
		"decider":     d.Decider,
		"duration_ms": round(totalMillis),
		"durations_ms": map[string]float64{
			"snapshot":  round(d.SnapshotMillis),
			"decider":   round(d.DeciderMillis),
			"score":     round(d.scoreMillis()),
			"normalize": round(normalizeMillis),
			"total":     round(totalMillis),
		},
		"event":     "decision",
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano),
	}
	write(line)
}

// logBinding records where the pod actually landed, which is the only trustworthy
// placement record when scores tie.
func (p *Plugin) logBinding(podUID, namespace, name, intended, bound string, tieCount int) {
	write(map[string]any{
		"run_id":         p.config.RunID,
		"integration":    Integration,
		"strategy":       p.strategyName(),
		"policy_version": p.config.PolicyVersion,
		"pod_uid":        podUID,
		"namespace":      namespace,
		"pod_name":       name,
		"intended_node":  nullable(intended),
		"bound_node":     bound,
		"tie_count":      tieCount,
		"matched":        intended == "" || intended == bound,
		"event":          "binding",
		"timestamp":      time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func write(line map[string]any) {
	encoded, err := json.Marshal(line)
	if err != nil {
		return
	}
	logMutex.Lock()
	defer logMutex.Unlock()
	os.Stdout.Write(append(encoded, '\n'))
}

func round(value float64) float64 {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return 0
	}
	return math.Round(value*1000) / 1000
}

func nullable(value string) any {
	if value == "" {
		return nil
	}
	return value
}

// validScores rejects anything that would make the ranking meaningless: a partial score
// map, an unknown node, a value that is not a finite number, or a value so large that the
// conversion to the framework's int64 scale would lose its meaning.
//
// Completeness matters: a missing candidate would be treated as zero and could outrank a
// node that was legitimately given a negative score.
func validScores(s *snapshot.Snapshot, scores map[string]float64) bool {
	if len(scores) != len(s.Nodes) {
		return false
	}
	for _, node := range s.Nodes {
		value, ok := scores[node.Name]
		if !ok {
			return false
		}
		if math.IsNaN(value) || math.IsInf(value, 0) || math.Abs(value) > maxRawScore {
			return false
		}
	}
	return true
}
