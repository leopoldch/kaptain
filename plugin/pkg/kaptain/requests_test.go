package kaptain

import (
	"context"
	"encoding/json"
	"os"
	"slices"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/kubernetes/pkg/scheduler/framework"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
	"github.com/leopoldch/kaptain/plugin/pkg/strategies"
	"github.com/leopoldch/kaptain/plugin/pkg/telemetry"
)

// testdata/pod-requests.json is read here only since the Python port left with the extender.
func TestPodRequestsMatchesTheSharedTable(t *testing.T) {
	raw, err := os.ReadFile("../../../testdata/pod-requests.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []struct {
			Name     string `json:"name"`
			Pod      v1.Pod `json:"pod"`
			Expected struct {
				MilliCPU    int64 `json:"millicpu"`
				MemoryBytes int64 `json:"memory_bytes"`
			} `json:"expected"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared pod-requests table is empty")
	}

	for _, testCase := range table.Cases {
		pod := testCase.Pod
		cpu, memory := podRequests(&pod)
		if cpu != testCase.Expected.MilliCPU || memory != testCase.Expected.MemoryBytes {
			t.Errorf("%s: got (%d millicpu, %d bytes), want (%d, %d)",
				testCase.Name, cpu, memory, testCase.Expected.MilliCPU, testCase.Expected.MemoryBytes)
		}
	}
}

// fakeTelemetry stands in for the metrics API in tests.
type fakeTelemetry map[string]telemetry.Usage

func (f fakeTelemetry) Get(node string) (telemetry.Usage, bool) {
	usage, ok := f[node]
	return usage, ok
}

func (f fakeTelemetry) Name() string { return "fake" }

func TestSnapshotCarriesMeasuredUsageWhenAvailable(t *testing.T) {
	p := &Plugin{
		config:       Config{ReservationTTL: time.Minute},
		strategy:     mustStrategy(t, strategies.LeastUsed),
		reservations: newReservations(time.Minute),
		telemetry:    fakeTelemetry{"measured": {MilliCPU: 900, MemoryBytes: 4096, AgeSeconds: 1.5}},
	}
	nodes := []*framework.NodeInfo{node("measured", "4", "8Gi"), node("unmeasured", "4", "8Gi")}

	current := p.buildSnapshot(pod("demo", "uid-30", "100m", "64Mi"), nodes)

	measured, absent := current.Nodes[0], current.Nodes[1]
	if measured.UsedMilliCPU != 900 || measured.TelemetryAgeSeconds != 1.5 {
		t.Fatalf("measured node carries %+v", measured)
	}
	if slices.Contains(measured.Missing, snapshot.FeatureTelemetry) {
		t.Error("a measured node still reports telemetry as missing")
	}
	if !slices.Contains(absent.Missing, snapshot.FeatureTelemetry) {
		t.Error("an unmeasured node does not report telemetry as missing")
	}
	if absent.UsedMilliCPU != 0 || absent.TelemetryAgeSeconds != 0 {
		t.Error("missing telemetry must not be invented")
	}
	// Requested resources come from the scheduler cache, so their age is zero here and the
	// two ages never share a field.
	if measured.RequestsAgeSeconds != 0 {
		t.Errorf("requests age %v, want 0 from the scheduler cache", measured.RequestsAgeSeconds)
	}
}

func TestLeastUsedRanksOnMeasuredUsage(t *testing.T) {
	p := &Plugin{
		config:       Config{ReservationTTL: time.Minute},
		strategy:     mustStrategy(t, strategies.LeastUsed),
		reservations: newReservations(time.Minute),
		telemetry: fakeTelemetry{
			"hot":  {MilliCPU: 3800, MemoryBytes: 1 << 30, AgeSeconds: 0.5},
			"cold": {MilliCPU: 200, MemoryBytes: 1 << 30, AgeSeconds: 0.5},
		},
	}
	// "hot" reserves nothing but is busy: a requests-only strategy would prefer it.
	nodes := []*framework.NodeInfo{node("hot", "4", "8Gi"), node("cold", "4", "8Gi")}
	registerMetrics()

	_, decision := score(t, p, nodes, pod("demo", "uid-31", "100m", "64Mi"))

	if decision.Fallback() {
		t.Fatalf("unexpected fallback: %s", decision.FallbackReason)
	}
	if decision.Intended != "cold" {
		t.Fatalf("intended %q, want cold", decision.Intended)
	}
}

func TestLeastUsedFallsBackWithoutTelemetry(t *testing.T) {
	p := &Plugin{
		config:       Config{ReservationTTL: time.Minute},
		strategy:     mustStrategy(t, strategies.LeastUsed),
		reservations: newReservations(time.Minute),
	}
	registerMetrics()
	nodes := []*framework.NodeInfo{node("a", "4", "8Gi"), node("b", "8", "8Gi")}

	_, decision := score(t, p, nodes, pod("demo", "uid-32", "100m", "64Mi"))

	if decision.FallbackReason != reasonStrategyError {
		t.Fatalf("reason %q, want %q", decision.FallbackReason, reasonStrategyError)
	}
}

func TestStrategyRequiringTelemetryIsRefusedWithoutIt(t *testing.T) {
	if missing := strategies.MissingFeatures(mustStrategy(t, strategies.LeastUsed),
		[]string{snapshot.FeatureAllocatable, snapshot.FeatureRequested}); len(missing) != 1 {
		t.Fatalf("missing features %v, want just telemetry", missing)
	}
	t.Setenv("KAPTAIN_STRATEGY", strategies.LeastUsed)
	if _, err := New(context.Background(), nil, nil); err == nil {
		t.Fatal("least-used was accepted without telemetry")
	}
}

func mustStrategy(t *testing.T, name string) strategies.Strategy {
	t.Helper()
	strategy, err := strategies.Get(name)
	if err != nil {
		t.Fatalf("Get(%q): %v", name, err)
	}
	return strategy
}
