package strategies

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
)

const fixtureDir = "../../../testdata/snapshots"

type fixture struct {
	Comment            string                      `json:"comment"`
	Snapshot           snapshot.Snapshot           `json:"snapshot"`
	Expected           map[string]string           `json:"expected"`
	ExpectedNormalized map[string]map[string]int64 `json:"expected_normalized"`
}

func load(t *testing.T) []fixture {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(fixtureDir, "*.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no fixtures found in %s: %v", fixtureDir, err)
	}
	out := make([]fixture, 0, len(paths))
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read %s: %v", path, err)
		}
		var decoded fixture
		if err := json.Unmarshal(raw, &decoded); err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}
		out = append(out, decoded)
	}
	return out
}

// TestFixturesSelectExpectedNode is the parity test: the Python extender runs the same
// fixtures and must reach the same node.
func TestFixturesSelectExpectedNode(t *testing.T) {
	for _, f := range load(t) {
		for name, expected := range f.Expected {
			strategy, err := Get(name)
			if err != nil {
				t.Fatalf("Get(%q): %v", name, err)
			}
			current := f.Snapshot
			scores, err := strategy.Scores(&current)
			if err != nil {
				t.Fatalf("%s on %q: %v", name, f.Comment, err)
			}
			if got := snapshot.Best(current.Names(), scores); got != expected {
				t.Errorf("%s on %q: selected %q, want %q", name, f.Comment, got, expected)
			}
		}
	}
}

func TestStrategiesRejectEmptyCandidates(t *testing.T) {
	for _, name := range Names() {
		strategy, err := Get(name)
		if err != nil {
			t.Fatalf("Get(%q): %v", name, err)
		}
		if _, err := strategy.Scores(&snapshot.Snapshot{}); err == nil {
			t.Errorf("%s accepted an empty candidate list", name)
		}
	}
}

func TestUnknownStrategyIsRejected(t *testing.T) {
	if _, err := Get("does-not-exist"); err == nil {
		t.Fatal("unknown strategy was accepted")
	}
}

func TestDummyRandomIsStableForOnePod(t *testing.T) {
	current := snapshot.Snapshot{
		Seed:  42,
		Pod:   snapshot.Pod{UID: "pod-1"},
		Nodes: []snapshot.Node{{Name: "a"}, {Name: "b"}, {Name: "c"}},
	}
	strategy, _ := Get(DummyRandom)
	first, err := strategy.Scores(&current)
	if err != nil {
		t.Fatal(err)
	}
	second, _ := strategy.Scores(&current)
	if snapshot.Best(current.Names(), first) != snapshot.Best(current.Names(), second) {
		t.Fatal("seeded draw is not stable across calls")
	}

	current.Seed = 43
	changed, _ := strategy.Scores(&current)
	if len(changed) != len(first) {
		t.Fatal("score map size changed with the seed")
	}
}

func TestLeastAllocatedPrefersTheEmptiestNode(t *testing.T) {
	current := snapshot.Snapshot{
		Pod: snapshot.Pod{RequestedMilliCPU: 500, RequestedMemoryBytes: 1 << 28},
		Nodes: []snapshot.Node{
			{Name: "busy", AllocatableMilliCPU: 4000, AllocatableMemoryBytes: 1 << 33,
				RequestedMilliCPU: 3500, RequestedMemoryBytes: 1 << 32},
			{Name: "idle", AllocatableMilliCPU: 4000, AllocatableMemoryBytes: 1 << 33},
		},
	}
	strategy, _ := Get(LeastAllocated)
	scores, err := strategy.Scores(&current)
	if err != nil {
		t.Fatal(err)
	}
	if got := snapshot.Best(current.Names(), scores); got != "idle" {
		t.Fatalf("selected %q, want idle", got)
	}
}

func TestLeastAllocatedRejectsNodeWithoutAllocatable(t *testing.T) {
	current := snapshot.Snapshot{Nodes: []snapshot.Node{{Name: "broken"}}}
	strategy, _ := Get(LeastAllocated)
	if _, err := strategy.Scores(&current); err == nil {
		t.Fatal("a node without allocatable resources was accepted")
	}
}
