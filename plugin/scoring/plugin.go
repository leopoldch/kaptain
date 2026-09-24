// Package scoring scores the candidates Kubernetes has already filtered, by asking an
// out-of-process decider. It expresses its decision as a score, never as a constraint.
//
// Every policy under study lives in that decider, in Python. What stays here is the
// integration -- snapshot, quantisation, logging -- and the fallback, which has to be here
// because it runs exactly when the decider is the thing that failed.
package scoring

import (
	"context"
	"fmt"
	"math"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/klog/v2"
	"k8s.io/kubernetes/pkg/scheduler/framework"

	"kaptain/decider"
	"kaptain/snapshot"
	"kaptain/telemetry"
)

// Name is how the plugin is enabled in a scheduler profile.
const Name = "KaptainScore"

const stateKey framework.StateKey = "kaptain.decision"

type Plugin struct {
	config    Config
	decider   *decider.Client
	telemetry telemetry.Source
}

var (
	_ framework.PreScorePlugin = &Plugin{}
	_ framework.ScorePlugin    = &Plugin{}
	_ framework.PostBindPlugin = &Plugin{}
)

func New(ctx context.Context, _ runtime.Object, handle framework.Handle) (framework.Plugin, error) {
	config, err := configFromEnv()
	if err != nil {
		return nil, fmt.Errorf("configuration: %w", err)
	}
	if config.DeciderURL == "" {
		return nil, fmt.Errorf("KAPTAIN_DECIDER_URL is required: every policy runs out of process")
	}
	registerMetrics()
	plugin := &Plugin{config: config, decider: decider.New(config.DeciderURL, config.DeciderTimeout)}

	if config.Telemetry != telemetry.Disabled {
		if handle == nil {
			return nil, fmt.Errorf("telemetry collector %q needs a scheduler handle", config.Telemetry)
		}
		source, err := telemetry.Open(ctx, config.Telemetry, telemetry.Options{
			Config:   handle.KubeConfig(),
			Interval: config.TelemetryEvery,
			MaxAge:   config.TelemetryMaxAge,
		})
		if err != nil {
			return nil, fmt.Errorf("telemetry: %w", err)
		}
		plugin.telemetry = source
	}

	klog.InfoS("kaptain plugin ready", "declaredStrategy", config.Strategy,
		"deciderURL", config.DeciderURL, "deciderTimeout", config.DeciderTimeout,
		"runID", config.RunID, "policyVersion", config.PolicyVersion,
		"features", plugin.features(), "telemetryCollector", plugin.telemetryName())
	return plugin, nil
}

func (p *Plugin) Name() string { return Name }

// strategyName is the run's declared identity: KAPTAIN_STRATEGY. It is the metric label, so
// it stays one value per deployment. What actually scored is DeciderStrategy on each
// decision, reported by the decider itself, and a disagreement is counted.
func (p *Plugin) strategyName() string { return p.config.Strategy }

// telemetryName is the collector actually in use, recorded with every run.
func (p *Plugin) telemetryName() string {
	if p.telemetry == nil {
		return telemetry.Disabled
	}
	return p.telemetry.Name()
}

// features says what this deployment can actually put in a snapshot.
func (p *Plugin) features() []string {
	features := []string{snapshot.FeatureAllocatable, snapshot.FeatureRequested}
	if p.telemetry != nil {
		features = append(features, snapshot.FeatureTelemetry)
	}
	return features
}

// PreScore decides once per pod, so Score stays a lookup and the cost lands in one phase.
func (p *Plugin) PreScore(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodes []*framework.NodeInfo) *framework.Status {
	started := time.Now()
	strategy := p.strategyName()

	if len(nodes) == 0 {
		decisionsTotal.WithLabelValues(strategy, "no_candidates").Inc()
		fallbackTotal.WithLabelValues(strategy, reasonNoCandidates).Inc()
		return framework.NewStatus(framework.Skip)
	}

	snapshotStarted := time.Now()
	current := p.buildSnapshot(pod, nodes)
	snapshotMillis := millis(snapshotStarted)
	snapshotDuration.WithLabelValues(strategy, "ok").Observe(snapshotMillis / 1000)
	candidateNodes.WithLabelValues(strategy).Observe(float64(len(current.Nodes)))
	for _, node := range current.Nodes {
		if node.TelemetryAgeSeconds > 0 {
			cacheAgeSeconds.WithLabelValues(p.telemetryName()).Observe(node.TelemetryAgeSeconds)
		}
		if node.RequestsAgeSeconds > 0 {
			cacheAgeSeconds.WithLabelValues(snapshot.FeatureRequested).Observe(node.RequestsAgeSeconds)
		}
	}

	decision := p.decide(ctx, current)
	decision.RequestsAgeMs, decision.TelemetryAgeMs = oldestAges(current)
	decision.SnapshotMillis = snapshotMillis
	decision.CandidateCount = len(current.Nodes)

	// Convert only the winner set to framework scores while every candidate is in hand.
	decision.scoreCandidates(current.Names())

	totalMillis := millis(started)
	scoreDuration.WithLabelValues(strategy, Integration, decision.status()).Observe(totalMillis / 1000)
	decisionsTotal.WithLabelValues(strategy, decision.status()).Inc()
	if decision.Fallback() {
		fallbackTotal.WithLabelValues(strategy, decision.FallbackReason).Inc()
	}
	p.logDecision(current, decision, totalMillis)

	state.Write(stateKey, decision)
	return nil
}

// decide falls back explicitly on error, timeout, unknown node or invalid score.
func (p *Plugin) decide(ctx context.Context, current *snapshot.Snapshot) *Decision {
	decision := &Decision{Strategy: p.strategyName()}

	deciderStarted := time.Now()
	scores, reported, err := p.decider.Decide(ctx, current)
	decision.DeciderMillis = millis(deciderStarted)
	decision.DeciderStrategy = reported
	if reported != "" && reported != decision.Strategy {
		strategyMismatchTotal.WithLabelValues(decision.Strategy, reported).Inc()
	}

	status := "ok"
	if err != nil {
		status = "error"
		decision.FallbackReason = reasonDeciderError
	} else if !validScores(current, scores) {
		status = "invalid"
		decision.FallbackReason = reasonInvalidScore
		invalidDecisionTotal.WithLabelValues(decision.Strategy, reasonInvalidScore).Inc()
	}
	deciderDuration.WithLabelValues(decision.Strategy, status).Observe(decision.DeciderMillis / 1000)

	if decision.Fallback() {
		klog.V(2).InfoS("kaptain fallback", "reason", decision.FallbackReason, "error", err,
			"strategy", decision.Strategy, "fallbackStrategy", FallbackName)
		scores = fallbackScores(current)
	}

	decision.Scores = scores
	decision.Intended = snapshot.Best(current.Names(), scores)
	if decision.Intended == "" {
		invalidDecisionTotal.WithLabelValues(decision.Strategy, reasonInvalidChoice).Inc()
		decision.Intended = current.Names()[0]
	}
	return decision
}

// Score is a lookup into what PreScore already converted; an unknown node is scored
// lowest rather than failing the scheduling cycle.
func (p *Plugin) Score(_ context.Context, state *framework.CycleState, _ *v1.Pod, nodeName string) (int64, *framework.Status) {
	decision, err := readDecision(state)
	if err != nil {
		invalidDecisionTotal.WithLabelValues(p.strategyName(), "missing_state").Inc()
		return framework.MinNodeScore, nil
	}
	score, ok := decision.FrameworkScores[nodeName]
	if !ok {
		invalidDecisionTotal.WithLabelValues(decision.Strategy, reasonInvalidChoice).Inc()
		return framework.MinNodeScore, nil
	}
	return score, nil
}

// ScoreExtensions returns nil because PreScore already produced framework-range scores.
func (p *Plugin) ScoreExtensions() framework.ScoreExtensions { return nil }

// PostBind records where the pod actually landed, which on a tie is not the intended node.
func (p *Plugin) PostBind(_ context.Context, state *framework.CycleState, pod *v1.Pod, nodeName string) {
	intended, ties := "", 0
	if decision, err := readDecision(state); err == nil {
		intended, ties = decision.Intended, decision.TieCount
		if intended != nodeName {
			boundMismatchTotal.WithLabelValues(decision.Strategy).Inc()
		}
	}
	p.logBinding(string(pod.UID), pod.Namespace, pod.Name, intended, nodeName, ties)
}

func readDecision(state *framework.CycleState) (*Decision, error) {
	data, err := state.Read(stateKey)
	if err != nil {
		return nil, err
	}
	decision, ok := data.(*Decision)
	if !ok {
		return nil, fmt.Errorf("unexpected state type %T", data)
	}
	return decision, nil
}

// Clone satisfies framework.StateData; the decision is read-only after PreScore.
func (d *Decision) Clone() framework.StateData { return d }

// oldestAges: a decision is only as fresh as its stalest input, per source.
func oldestAges(current *snapshot.Snapshot) (requests, telemetry float64) {
	for _, node := range current.Nodes {
		requests = math.Max(requests, node.RequestsAgeSeconds*1000)
		telemetry = math.Max(telemetry, node.TelemetryAgeSeconds*1000)
	}
	return requests, telemetry
}

func millis(since time.Time) float64 {
	return float64(time.Since(since).Microseconds()) / 1000
}
