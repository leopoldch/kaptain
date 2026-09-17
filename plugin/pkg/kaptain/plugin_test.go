package kaptain

import (
	"context"
	"math"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/kubernetes/pkg/scheduler/framework"

	"github.com/leopoldch/kaptain/plugin/pkg/decider"
	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
	"github.com/leopoldch/kaptain/plugin/pkg/strategies"
)

func pod(name, uid, cpu, memory string) *v1.Pod {
	return &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default", UID: types.UID(uid)},
		Spec: v1.PodSpec{Containers: []v1.Container{{
			Name: "app",
			Resources: v1.ResourceRequirements{Requests: v1.ResourceList{
				v1.ResourceCPU:    resource.MustParse(cpu),
				v1.ResourceMemory: resource.MustParse(memory),
			}},
		}}},
	}
}

func node(name, cpu, memory string) *framework.NodeInfo {
	info := framework.NewNodeInfo()
	info.SetNode(&v1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: v1.NodeStatus{
			Allocatable: v1.ResourceList{
				v1.ResourceCPU:    resource.MustParse(cpu),
				v1.ResourceMemory: resource.MustParse(memory),
			},
			Conditions: []v1.NodeCondition{{Type: v1.NodeReady, Status: v1.ConditionTrue}},
		},
	})
	return info
}

func newPlugin(t *testing.T, strategy string, client *decider.Client) *Plugin {
	t.Helper()
	selected, err := strategies.Get(strategy)
	if err != nil {
		t.Fatalf("strategy %q: %v", strategy, err)
	}
	registerMetrics()
	return &Plugin{
		config:       Config{Strategy: strategy, Seed: 7, RunID: "test", PolicyVersion: "test", ReservationTTL: time.Minute},
		strategy:     selected,
		decider:      client,
		reservations: newReservations(time.Minute),
	}
}

func score(t *testing.T, p *Plugin, nodes []*framework.NodeInfo, target *v1.Pod) (framework.NodeScoreList, *Decision) {
	t.Helper()
	state := framework.NewCycleState()
	if status := p.PreScore(context.Background(), state, target, nodes); !status.IsSuccess() {
		t.Fatalf("PreScore failed: %v", status)
	}
	scores := framework.NodeScoreList{}
	for _, candidate := range nodes {
		value, status := p.Score(context.Background(), state, target, candidate.GetName())
		if !status.IsSuccess() {
			t.Fatalf("Score failed: %v", status)
		}
		scores = append(scores, framework.NodeScore{Name: candidate.GetName(), Score: value})
	}
	if status := p.NormalizeScore(context.Background(), state, target, scores); !status.IsSuccess() {
		t.Fatalf("NormalizeScore failed: %v", status)
	}
	decision, err := readDecision(state)
	if err != nil {
		t.Fatalf("decision missing from state: %v", err)
	}
	return scores, decision
}

// strategy returning a partial or absurd score map, to exercise validation.
type broken struct {
	name   string
	scores map[string]float64
}

func (b broken) Name() string { return b.name }

func (b broken) Scores(*snapshot.Snapshot) (map[string]float64, error) { return b.scores, nil }

func (b broken) Requires() []string { return nil }

func TestSelectedNodeGetsTheHighestNormalizedScore(t *testing.T) {
	p := newPlugin(t, strategies.LeastAllocated, nil)
	busy := node("busy", "4", "8Gi")
	busy.AddPod(pod("other", "uid-other", "3500m", "7Gi"))
	nodes := []*framework.NodeInfo{busy, node("idle", "4", "8Gi")}

	scores, decision := score(t, p, nodes, pod("demo", "uid-1", "500m", "256Mi"))

	if decision.Intended != "idle" {
		t.Fatalf("selected %q, want idle", decision.Intended)
	}
	for _, entry := range scores {
		if entry.Name == decision.Intended && entry.Score != framework.MaxNodeScore {
			t.Fatalf("selected node scored %d, want %d", entry.Score, framework.MaxNodeScore)
		}
		if entry.Score < framework.MinNodeScore || entry.Score > framework.MaxNodeScore {
			t.Fatalf("node %s scored %d, outside the Kubernetes range", entry.Name, entry.Score)
		}
	}
}

func TestEqualScoresNormaliseToMax(t *testing.T) {
	p := newPlugin(t, strategies.LargestCPUCapacity, nil)
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "4", "8Gi")}
	scores, _ := score(t, p, nodes, pod("demo", "uid-2", "100m", "64Mi"))
	for _, entry := range scores {
		if entry.Score != framework.MaxNodeScore {
			t.Fatalf("node %s scored %d on a tie, want %d", entry.Name, entry.Score, framework.MaxNodeScore)
		}
	}
}

func TestNoCandidatesSkipsWithoutFallingBack(t *testing.T) {
	p := newPlugin(t, strategies.DummyRandom, nil)
	state := framework.NewCycleState()
	status := p.PreScore(context.Background(), state, pod("demo", "uid-3", "100m", "64Mi"), nil)
	if status.Code() != framework.Skip {
		t.Fatalf("got %v, want Skip", status.Code())
	}
	if _, err := readDecision(state); err == nil {
		t.Fatal("a decision was written without candidates")
	}
}

func TestUnknownNodeScoresLowestInsteadOfFailing(t *testing.T) {
	p := newPlugin(t, strategies.DummyRandom, nil)
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi")}
	state := framework.NewCycleState()
	if status := p.PreScore(context.Background(), state, pod("demo", "uid-4", "100m", "64Mi"), nodes); !status.IsSuccess() {
		t.Fatalf("PreScore failed: %v", status)
	}
	value, status := p.Score(context.Background(), state, pod("demo", "uid-4", "100m", "64Mi"), "ghost")
	if !status.IsSuccess() {
		t.Fatalf("Score failed on an unknown node: %v", status)
	}
	if value != framework.MinNodeScore {
		t.Fatalf("unknown node scored %d, want %d", value, framework.MinNodeScore)
	}
}

func TestMissingStateScoresLowestInsteadOfFailing(t *testing.T) {
	p := newPlugin(t, strategies.DummyRandom, nil)
	value, status := p.Score(context.Background(), framework.NewCycleState(), pod("demo", "uid-5", "100m", "64Mi"), "a")
	if !status.IsSuccess() || value != framework.MinNodeScore {
		t.Fatalf("got (%d, %v), want (%d, success)", value, status, framework.MinNodeScore)
	}
}

func TestDeciderTimeoutFallsBackExplicitly(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(120 * time.Millisecond)
		w.Write([]byte(`{"node":"idle"}`))
	}))
	t.Cleanup(server.Close)

	p := newPlugin(t, strategies.DummyRandom, decider.New(server.URL, 15*time.Millisecond))
	busy := node("busy", "4", "8Gi")
	busy.AddPod(pod("other", "uid-other", "3800m", "7Gi"))
	nodes := []*framework.NodeInfo{busy, node("idle", "4", "8Gi")}

	_, decision := score(t, p, nodes, pod("demo", "uid-6", "500m", "256Mi"))

	if !decision.Fallback() || decision.FallbackReason != reasonDeciderError {
		t.Fatalf("got fallback=%v reason=%q, want a decider_error fallback", decision.Fallback(), decision.FallbackReason)
	}
	if decision.Intended != "idle" {
		t.Fatalf("fallback selected %q, want idle (most free requests)", decision.Intended)
	}
}

func TestDeciderInvalidNodeFallsBackExplicitly(t *testing.T) {
	p := newPlugin(t, strategies.DummyRandom, decider.New("http://127.0.0.1:1/choose", 10*time.Millisecond))
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "8", "8Gi")}
	_, decision := score(t, p, nodes, pod("demo", "uid-7", "100m", "64Mi"))
	if decision.FallbackReason != reasonDeciderError {
		t.Fatalf("reason %q, want %q", decision.FallbackReason, reasonDeciderError)
	}
	if decision.Intended == "" {
		t.Fatal("fallback selected nothing")
	}
}

func TestStrategyFailureFallsBackExplicitly(t *testing.T) {
	p := newPlugin(t, strategies.LeastAllocated, nil)
	// A node without allocatable resources makes least-allocated fail.
	broken := framework.NewNodeInfo()
	broken.SetNode(&v1.Node{ObjectMeta: metav1.ObjectMeta{Name: "broken"}})
	nodes := []*framework.NodeInfo{broken, node("ok", "4", "8Gi")}

	_, decision := score(t, p, nodes, pod("demo", "uid-8", "100m", "64Mi"))

	if decision.FallbackReason != reasonStrategyError {
		t.Fatalf("reason %q, want %q", decision.FallbackReason, reasonStrategyError)
	}
	if decision.Intended != "ok" {
		t.Fatalf("fallback selected %q, want ok", decision.Intended)
	}
}

func TestReserveIsAccountedInTheNextDecision(t *testing.T) {
	p := newPlugin(t, strategies.LeastAllocated, nil)
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "4", "8Gi")}

	first := pod("first", "uid-9", "3500m", "7Gi")
	_, decision := score(t, p, nodes, first)
	chosen := decision.Intended

	if status := p.Reserve(context.Background(), framework.NewCycleState(), first, chosen); !status.IsSuccess() {
		t.Fatalf("Reserve failed: %v", status)
	}

	_, second := score(t, p, nodes, pod("second", "uid-10", "500m", "256Mi"))
	if second.Intended == chosen {
		t.Fatalf("second pod also chose %q: the reservation was ignored", chosen)
	}

	p.Unreserve(context.Background(), framework.NewCycleState(), first, chosen)
	_, third := score(t, p, nodes, pod("third", "uid-11", "500m", "256Mi"))
	if third.Intended != chosen {
		t.Fatalf("after Unreserve the node %q is still avoided (got %q)", chosen, third.Intended)
	}
}

func TestExpiredReservationIsDropped(t *testing.T) {
	p := newPlugin(t, strategies.LeastAllocated, nil)
	p.reservations = newReservations(time.Nanosecond)
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "4", "8Gi")}

	held := pod("held", "uid-12", "3900m", "7Gi")
	p.Reserve(context.Background(), framework.NewCycleState(), held, "a")
	time.Sleep(time.Millisecond)

	_, decision := score(t, p, nodes, pod("next", "uid-13", "500m", "256Mi"))
	if decision.Intended != "a" {
		t.Fatalf("selected %q: an expired reservation is still counted", decision.Intended)
	}
}

func TestPartialScoreMapFallsBack(t *testing.T) {
	p := newPlugin(t, strategies.DummyRandom, nil)
	// "a" is missing: treated as zero it could outrank a legitimately negative score.
	p.strategy = broken{name: "partial", scores: map[string]float64{"b": -5}}
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "8", "8Gi")}

	_, decision := score(t, p, nodes, pod("demo", "uid-20", "100m", "64Mi"))

	if decision.FallbackReason != reasonInvalidScore {
		t.Fatalf("reason %q, want %q", decision.FallbackReason, reasonInvalidScore)
	}
}

func TestAbsurdScoreFallsBack(t *testing.T) {
	p := newPlugin(t, strategies.DummyRandom, nil)
	p.strategy = broken{name: "absurd", scores: map[string]float64{"a": 1e300, "b": 1}}
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "8", "8Gi")}

	_, decision := score(t, p, nodes, pod("demo", "uid-21", "100m", "64Mi"))

	if decision.FallbackReason != reasonInvalidScore {
		t.Fatalf("reason %q, want %q", decision.FallbackReason, reasonInvalidScore)
	}
}

func TestScaledScoreStaysInInt64Range(t *testing.T) {
	if got := scaled(math.Inf(1)); got <= 0 || got > int64(maxRawScore*scorePrecision) {
		t.Fatalf("scaled(+Inf) = %d, outside the clamp", got)
	}
	if got := scaled(math.Inf(-1)); got >= 0 || got < -int64(maxRawScore*scorePrecision) {
		t.Fatalf("scaled(-Inf) = %d, outside the clamp", got)
	}
	if got := scaled(math.NaN()); got != framework.MinNodeScore {
		t.Fatalf("scaled(NaN) = %d, want %d", got, framework.MinNodeScore)
	}
}

func TestNegativeScoresRankBelowZero(t *testing.T) {
	p := newPlugin(t, strategies.DummyRandom, nil)
	p.strategy = broken{name: "negative", scores: map[string]float64{"a": -1, "b": 0}}
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "8", "8Gi")}

	scores, decision := score(t, p, nodes, pod("demo", "uid-22", "100m", "64Mi"))

	if decision.Fallback() {
		t.Fatalf("a complete map of finite scores was rejected: %q", decision.FallbackReason)
	}
	if decision.Intended != "b" {
		t.Fatalf("intended %q, want b", decision.Intended)
	}
	for _, entry := range scores {
		if entry.Name == "a" && entry.Score != framework.MinNodeScore {
			t.Fatalf("negative score normalised to %d, want %d", entry.Score, framework.MinNodeScore)
		}
	}
}

func TestTotalDurationCoversNormalisation(t *testing.T) {
	p := newPlugin(t, strategies.LargestCPUCapacity, nil)
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "8", "8Gi")}
	_, decision := score(t, p, nodes, pod("demo", "uid-23", "100m", "64Mi"))

	if decision.scoreMillis() <= 0 {
		t.Fatal("per-node scoring time was not accumulated")
	}
	total := millis(decision.Started)
	if total < decision.SnapshotMillis+decision.scoreMillis() {
		t.Fatalf("total %.3fms is smaller than its own phases", total)
	}
}

func TestTieCountIsRecorded(t *testing.T) {
	p := newPlugin(t, strategies.LargestCPUCapacity, nil)
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "4", "8Gi")}
	_, decision := score(t, p, nodes, pod("demo", "uid-24", "100m", "64Mi"))
	if decision.TieCount != 2 {
		t.Fatalf("tie_count = %d, want 2: both nodes share the top score", decision.TieCount)
	}
}

func TestPostBindRecordsTheBoundNode(t *testing.T) {
	p := newPlugin(t, strategies.LargestCPUCapacity, nil)
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "4", "8Gi")}
	target := pod("demo", "uid-25", "100m", "64Mi")

	state := framework.NewCycleState()
	if status := p.PreScore(context.Background(), state, target, nodes); !status.IsSuccess() {
		t.Fatalf("PreScore failed: %v", status)
	}
	// A tie: the scheduler may bind a node other than the intended one.
	p.PostBind(context.Background(), state, target, "b")

	decision, err := readDecision(state)
	if err != nil {
		t.Fatal(err)
	}
	if decision.Intended == "" {
		t.Fatal("no intended node recorded")
	}
}
