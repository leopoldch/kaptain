package scoring

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"k8s.io/kubernetes/pkg/scheduler/framework"

	"kaptain/snapshot"
)

// Integration labels every result line and metric sample.
const Integration = "plugin"

// maxRawScore rejects absurd values instead of ranking on them.
const maxRawScore = 1e9

// Fallback reasons: a closed set, because they are metric label values.
const (
	reasonDeciderError  = "decider_error"
	reasonInvalidChoice = "invalid_choice"
	reasonInvalidScore  = "invalid_score"
	reasonNoCandidates  = "no_candidates"
	reasonMismatch      = "strategy_mismatch"
)

// Decision is what PreScore computes once per pod and Score then reads per node.
type Decision struct {
	// PolicyScores is what the decider answered, kept even when it was refused: on a
	// fallback, Scores holds the fallback's and the policy's answer would otherwise be lost
	// -- which is exactly the run someone will want to explain afterwards.
	PolicyScores map[string]float64

	Scores   map[string]float64
	Intended string
	Strategy string

	// DeciderStrategy is what the remote policy called itself, empty when it ran locally.
	DeciderStrategy string

	FallbackReason string
	CandidateCount int
	RequestsAgeMs  float64
	TelemetryAgeMs float64

	SnapshotMillis   float64
	DeciderMillis    float64
	MaxSourceStaleMs float64
	MaxSourceDeltaMs float64

	// FrameworkScores is what Score returns, computed once while all candidates are in hand.
	FrameworkScores map[string]int64
	TieCount        int

	// FallbackBlindNodes are the candidates the fallback ranked without their allocatable
	// or requested resources, which it reads as zero because it must still answer.
	FallbackBlindNodes []string

	// Winners is every node that shares the top score. kube-scheduler picks among them at
	// random, so a binding on any of them conformed to the policy; only the tie-break
	// differed. Comparing against Intended alone would call that a mismatch.
	Winners []string

	// ID identifies this scheduling attempt, not the pod: a pod that fails to bind comes
	// back through a new cycle and produces a second decision. The binding line carries it,
	// so an attempt can be reconstructed afterwards from its own inputs and its own result.
	ID string
}

// newDecisionID carries the instance as well as the run: the counter restarts with the
// process while RUN_ID does not, so run and counter alone collide after a restart.
func newDecisionID(runID, instanceID string) string {
	return fmt.Sprintf("%s-%s-%d", runID, instanceID, decisionCounter.Add(1))
}

var decisionCounter atomic.Int64

func (d *Decision) Fallback() bool { return d.FallbackReason != "" }

func (d *Decision) status() string {
	if d.Fallback() {
		return "fallback"
	}
	return "ok"
}

// scoreCandidates gives the top-scoring nodes the framework maximum and the rest zero.
// Only the top score affects placement; on a tie kube-scheduler picks among them at random.
func (d *Decision) scoreCandidates(candidates []string) {
	highest := math.Inf(-1)
	for _, name := range candidates {
		highest = math.Max(highest, d.Scores[name])
	}
	d.FrameworkScores = make(map[string]int64, len(candidates))
	d.Winners = d.Winners[:0]
	for _, name := range candidates {
		if d.Scores[name] == highest {
			d.FrameworkScores[name] = framework.MaxNodeScore
			d.Winners = append(d.Winners, name)
			d.TieCount++
		} else {
			d.FrameworkScores[name] = framework.MinNodeScore
		}
	}
}

var logMutex sync.Mutex

// logDecision records the complete decision timings. intended_node is what the policy chose,
// not where the pod lands: see PostBind.
func (p *Plugin) logDecision(s *snapshot.Snapshot, d *Decision, totalMillis float64) {
	line := map[string]any{
		"decision_id":      d.ID,
		"run_id":           s.RunID,
		"integration":      Integration,
		"strategy":         d.Strategy,
		"policy_version":   s.PolicyVersion,
		"pod_uid":          s.Pod.UID,
		"namespace":        s.Pod.Namespace,
		"pod_name":         s.Pod.Name,
		"candidate_count":  d.CandidateCount,
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
		"decider_strategy":     nullable(d.DeciderStrategy),
		"fallback_blind_nodes": d.FallbackBlindNodes,
		// The freshness of what the decision actually ran on, kept here because the
		// snapshot itself is not persisted: "this placement used a 27 s old measurement"
		// has to be answerable after the run.
		"max_source_stale_ms":           round(d.MaxSourceStaleMs),
		"max_source_timestamp_delta_ms": round(d.MaxSourceDeltaMs),
		"duration_ms":                   round(totalMillis),
		// Every candidate's raw score, not only the winner's: without them a surprising
		// placement cannot be explained after the fact.
		"effective_scores": d.Scores,
		"policy_scores":    d.PolicyScores,
		"durations_ms": map[string]float64{
			"snapshot": round(d.SnapshotMillis),
			"decider":  round(d.DeciderMillis),
			"total":    round(totalMillis),
		},
		"event":     "decision",
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano),
	}
	write(line)
}

// logBinding is the only trustworthy placement record when scores tie.
func (p *Plugin) logBinding(decisionID, podUID, namespace, name, intended, bound string,
	winners []string, tieCount int) {
	write(map[string]any{
		"decision_id":    nullable(decisionID),
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
		"winner_nodes":   winners,
		"scored":         intended != "",
		// Every winner was an acceptable placement: kube-scheduler picks among them at
		// random, so equality with intended_node would count its tie-break as a mismatch.
		"matched":   intended != "" && contains(winners, bound),
		"event":     "binding",
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano),
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

func contains(names []string, wanted string) bool {
	for _, name := range names {
		if name == wanted {
			return true
		}
	}
	return false
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

// validScores requires completeness: a missing candidate would be read as zero and could
// outrank a node legitimately given a negative score.
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
