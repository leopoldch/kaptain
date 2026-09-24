// Package kaptain scores the candidates Kubernetes has already filtered. It expresses its
// decision as a score, never as a constraint.
package kaptain

import (
	"context"
	"fmt"
	"math"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/klog/v2"
	extenderv1 "k8s.io/kube-scheduler/extender/v1"
	"k8s.io/kubernetes/pkg/scheduler/framework"

	"github.com/leopoldch/kaptain/plugin/pkg/decider"
	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
	"github.com/leopoldch/kaptain/plugin/pkg/strategies"
	"github.com/leopoldch/kaptain/plugin/pkg/telemetry"
)

// Name is how the plugin is enabled in a scheduler profile.
const Name = "KaptainScore"

// scorePrecision turns a float score into the int64 the framework expects.
const scorePrecision = 1000

// scoreStep is the factor kube-scheduler itself applies to extender scores.
const scoreStep = framework.MaxNodeScore / extenderv1.MaxExtenderPriority

const (
	stateKey    framework.StateKey = "kaptain.decision"
	snapshotKey framework.StateKey = "kaptain.snapshot"
)

type Plugin struct {
	config       Config
	strategy     strategies.Strategy
	decider      *decider.Client
	reservations *reservations
	telemetry    telemetry.Source
}

var (
	_ framework.PreScorePlugin = &Plugin{}
	_ framework.ScorePlugin    = &Plugin{}
	_ framework.ReservePlugin  = &Plugin{}
	_ framework.PostBindPlugin = &Plugin{}
)

func New(ctx context.Context, _ runtime.Object, handle framework.Handle) (framework.Plugin, error) {
	config, err := configFromEnv()
	if err != nil {
		return nil, fmt.Errorf("configuration: %w", err)
	}
	strategy, err := strategies.Get(config.Strategy)
	if err != nil {
		return nil, err
	}
	registerMetrics()

	plugin := &Plugin{
		config:       config,
		strategy:     strategy,
		reservations: newReservations(config.ReservationTTL),
	}

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

	if missing := strategies.MissingFeatures(strategy, plugin.features()); len(missing) > 0 {
		return nil, fmt.Errorf("strategy %q needs %v; set KAPTAIN_TELEMETRY=%s", strategy.Name(), missing, telemetry.MetricsAPI)
	}
	if config.DeciderURL != "" {
		plugin.decider = decider.New(config.DeciderURL, config.DeciderTimeout)
	}
	klog.InfoS("kaptain plugin ready", "strategy", config.Strategy, "decider", plugin.deciderName(),
		"deciderTimeout", config.DeciderTimeout, "runID", config.RunID, "policyVersion", config.PolicyVersion,
		"features", plugin.features(), "telemetryCollector", plugin.telemetryName())
	return plugin, nil
}

func (p *Plugin) Name() string { return Name }

func (p *Plugin) strategyName() string { return p.strategy.Name() }

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

func (p *Plugin) deciderName() string {
	if p.decider == nil {
		return "local"
	}
	return "external"
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
	decision.Started = started
	decision.RequestsAgeMs, decision.TelemetryAgeMs = oldestAges(current)
	decision.SnapshotMillis = snapshotMillis
	decision.CandidateCount = len(current.Nodes)

	state.Write(stateKey, decision)
	state.Write(snapshotKey, &snapshotState{snapshot: current})
	return nil
}

// decide falls back explicitly on error, timeout, unknown node or invalid score.
func (p *Plugin) decide(ctx context.Context, current *snapshot.Snapshot) *Decision {
	decision := &Decision{Strategy: p.strategyName(), Decider: p.deciderName()}

	deciderStarted := time.Now()
	scores, err := p.rawScores(ctx, current)
	decision.DeciderMillis = millis(deciderStarted)

	status := "ok"
	if err != nil {
		status = "error"
		decision.FallbackReason = p.failureReason(err)
	} else if !validScores(current, scores) {
		status = "invalid"
		decision.FallbackReason = reasonInvalidScore
		invalidDecisionTotal.WithLabelValues(decision.Strategy, reasonInvalidScore).Inc()
	}
	deciderDuration.WithLabelValues(decision.Strategy, decision.Decider, status).Observe(decision.DeciderMillis / 1000)

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

func (p *Plugin) rawScores(ctx context.Context, current *snapshot.Snapshot) (map[string]float64, error) {
	if p.decider != nil {
		return p.decider.Decide(ctx, current)
	}
	return p.strategy.Scores(current)
}

func (p *Plugin) failureReason(err error) string {
	if err == nil {
		return ""
	}
	if p.decider != nil {
		return reasonDeciderError
	}
	return reasonStrategyError
}

// Score is a lookup; an unknown node is scored lowest rather than failing the cycle.
func (p *Plugin) Score(_ context.Context, state *framework.CycleState, _ *v1.Pod, nodeName string) (int64, *framework.Status) {
	started := time.Now()
	decision, err := readDecision(state)
	if err != nil {
		invalidDecisionTotal.WithLabelValues(p.strategyName(), "missing_state").Inc()
		return framework.MinNodeScore, nil
	}
	defer func() { decision.addScoreTime(time.Since(started)) }()

	score, ok := decision.Scores[nodeName]
	if !ok {
		invalidDecisionTotal.WithLabelValues(decision.Strategy, reasonInvalidChoice).Inc()
		return framework.MinNodeScore, nil
	}
	return scaled(score), nil
}

// scaled clamps as well as converts, so a rogue value cannot overflow the int64 scale.
func scaled(score float64) int64 {
	if math.IsNaN(score) {
		return framework.MinNodeScore
	}
	product := score * scorePrecision
	if product > maxRawScore*scorePrecision {
		product = maxRawScore * scorePrecision
	}
	if product < -maxRawScore*scorePrecision {
		product = -maxRawScore * scorePrecision
	}
	return int64(math.Round(product))
}

func (p *Plugin) ScoreExtensions() framework.ScoreExtensions { return p }

// NormalizeScore is where the decision is logged and timed: by now every phase has run.
// It quantises the raw values, so scores below 1/scorePrecision do not collapse.
func (p *Plugin) NormalizeScore(_ context.Context, state *framework.CycleState, _ *v1.Pod, scores framework.NodeScoreList) *framework.Status {
	started := time.Now()

	decision, err := readDecision(state)
	if err != nil {
		normalizeFromInt64(scores)
		normalizeDuration.WithLabelValues(p.strategyName()).Observe(millis(started) / 1000)
		invalidDecisionTotal.WithLabelValues(p.strategyName(), "missing_state").Inc()
		return nil
	}

	lowest, highest := math.Inf(1), math.Inf(-1)
	for _, entry := range scores {
		raw := decision.Scores[entry.Name]
		lowest, highest = math.Min(lowest, raw), math.Max(highest, raw)
	}
	for i := range scores {
		raw := decision.Scores[scores[i].Name]
		scores[i].Score = QuantizeRaw(raw, lowest, highest) * scoreStep
	}

	ties := 0
	for _, entry := range scores {
		if entry.Score == framework.MaxNodeScore {
			ties++
		}
	}
	normalizeMillis := millis(started)

	decision.TieCount = ties
	normalizeDuration.WithLabelValues(decision.Strategy).Observe(normalizeMillis / 1000)

	totalMillis := millis(decision.Started)
	scoreDuration.WithLabelValues(decision.Strategy, Integration, decision.status()).Observe(totalMillis / 1000)
	decisionsTotal.WithLabelValues(decision.Strategy, decision.status()).Inc()
	if decision.Fallback() {
		fallbackTotal.WithLabelValues(decision.Strategy, decision.FallbackReason).Inc()
	}

	if current, err := readSnapshot(state); err == nil {
		p.logDecision(current, decision, normalizeMillis, totalMillis)
	}
	return nil
}

// QuantizeRaw truncates onto [0, MaxExtenderPriority]; bridge/snapshot.py mirrors it, and
// testdata/quantisation.json checks that both agree.
func QuantizeRaw(score, lowest, highest float64) int64 {
	if highest == lowest {
		return extenderv1.MaxExtenderPriority
	}
	return int64((score - lowest) * float64(extenderv1.MaxExtenderPriority) / (highest - lowest))
}

// normalizeFromInt64 is the degraded path: the decision is gone, so only int64 scores remain.
func normalizeFromInt64(scores framework.NodeScoreList) {
	lowest, highest := scores[0].Score, scores[0].Score
	for _, entry := range scores {
		if entry.Score < lowest {
			lowest = entry.Score
		}
		if entry.Score > highest {
			highest = entry.Score
		}
	}
	for i := range scores {
		if highest == lowest {
			scores[i].Score = framework.MaxNodeScore
			continue
		}
		scores[i].Score = (scores[i].Score - lowest) * extenderv1.MaxExtenderPriority / (highest - lowest) * scoreStep
	}
}

// Reserve records the assignment locally; it reserves nothing in the cluster.
func (p *Plugin) Reserve(_ context.Context, _ *framework.CycleState, pod *v1.Pod, nodeName string) *framework.Status {
	p.reservations.add(pod, nodeName)
	return nil
}

func (p *Plugin) Unreserve(_ context.Context, _ *framework.CycleState, pod *v1.Pod, _ string) {
	p.reservations.remove(pod)
}

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

type snapshotState struct {
	snapshot *snapshot.Snapshot
}

func (s *snapshotState) Clone() framework.StateData { return s }

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

func readSnapshot(state *framework.CycleState) (*snapshot.Snapshot, error) {
	data, err := state.Read(snapshotKey)
	if err != nil {
		return nil, err
	}
	held, ok := data.(*snapshotState)
	if !ok {
		return nil, fmt.Errorf("unexpected state type %T", data)
	}
	return held.snapshot, nil
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
