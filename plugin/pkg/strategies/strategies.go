// Package strategies holds the local placement policies of the plugin. A strategy is a
// pure function of the snapshot: no cluster calls, no training, no state. The Python
// extender implements the same rules on the same snapshot fields.
package strategies

import (
	"fmt"
	"sort"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
)

type Strategy interface {
	Name() string
	// Scores returns a raw score per node name. Higher is better.
	Scores(s *snapshot.Snapshot) (map[string]float64, error)
	// Requires lists the snapshot feature groups the strategy cannot work without, so a
	// deployment that cannot provide them is refused at startup instead of producing a run
	// made only of fallbacks.
	Requires() []string
}

// MissingFeatures reports what a strategy needs and the deployment cannot provide.
func MissingFeatures(strategy Strategy, available []string) []string {
	have := map[string]bool{}
	for _, feature := range available {
		have[feature] = true
	}
	missing := []string{}
	for _, feature := range strategy.Requires() {
		if !have[feature] {
			missing = append(missing, feature)
		}
	}
	sort.Strings(missing)
	return missing
}

const (
	DummyRandom        = "dummy-random"
	LargestCPUCapacity = "largest-cpu-capacity"
	LeastAllocated     = "least-allocated"
	LeastUsed          = "least-used"
)

func Get(name string) (Strategy, error) {
	switch name {
	case DummyRandom:
		return dummyRandom{}, nil
	case LargestCPUCapacity:
		return largestCPUCapacity{}, nil
	case LeastAllocated:
		return leastAllocated{}, nil
	case LeastUsed:
		return leastUsed{}, nil
	default:
		return nil, fmt.Errorf("unknown strategy %q, known: %v", name, Names())
	}
}

func Names() []string {
	names := []string{DummyRandom, LargestCPUCapacity, LeastAllocated, LeastUsed}
	sort.Strings(names)
	return names
}

// dummyRandom is the control arm: a seeded draw, reproducible across runs and across
// both integrations, keyed on the pod UID so one pod always gets the same node.
type dummyRandom struct{}

func (dummyRandom) Name() string { return DummyRandom }

func (dummyRandom) Requires() []string { return nil }

func (dummyRandom) Scores(s *snapshot.Snapshot) (map[string]float64, error) {
	if len(s.Nodes) == 0 {
		return nil, fmt.Errorf("no candidate nodes")
	}
	chosen := snapshot.Pick(s.Names(), s.Seed, s.Pod.TaskID)
	scores := make(map[string]float64, len(s.Nodes))
	for _, n := range s.Nodes {
		scores[n.Name] = 0
	}
	scores[chosen] = 1
	return scores, nil
}

// largestCPUCapacity ranks on static allocatable CPU. It does not spread and ignores what
// is already running: kept because it is the historical kaptain strategy.
type largestCPUCapacity struct{}

func (largestCPUCapacity) Name() string { return LargestCPUCapacity }

func (largestCPUCapacity) Requires() []string { return []string{snapshot.FeatureAllocatable} }

func (largestCPUCapacity) Scores(s *snapshot.Snapshot) (map[string]float64, error) {
	if len(s.Nodes) == 0 {
		return nil, fmt.Errorf("no candidate nodes")
	}
	scores := make(map[string]float64, len(s.Nodes))
	for _, n := range s.Nodes {
		scores[n.Name] = float64(n.AllocatableMilliCPU)
	}
	return scores, nil
}

// leastAllocated is the resource-aware baseline: it prefers the node left with the most
// free CPU and memory once the incoming pod's requests are added, as the native
// NodeResourcesFit LeastAllocated scorer does. It needs per-node requested resources,
// which the plugin reads from the scheduler cache.
type leastAllocated struct{}

func (leastAllocated) Name() string { return LeastAllocated }

func (leastAllocated) Requires() []string {
	return []string{snapshot.FeatureAllocatable, snapshot.FeatureRequested}
}

func (leastAllocated) Scores(s *snapshot.Snapshot) (map[string]float64, error) {
	if len(s.Nodes) == 0 {
		return nil, fmt.Errorf("no candidate nodes")
	}
	scores := make(map[string]float64, len(s.Nodes))
	for _, n := range s.Nodes {
		if n.AllocatableMilliCPU <= 0 || n.AllocatableMemoryBytes <= 0 {
			return nil, fmt.Errorf("node %q has no allocatable CPU or memory", n.Name)
		}
		cpu := free(n.AllocatableMilliCPU, n.RequestedMilliCPU+s.Pod.RequestedMilliCPU)
		memory := free(n.AllocatableMemoryBytes, n.RequestedMemoryBytes+s.Pod.RequestedMemoryBytes)
		scores[n.Name] = (cpu + memory) / 2
	}
	return scores, nil
}

func free(allocatable, requested int64) float64 {
	if requested >= allocatable {
		return 0
	}
	return float64(allocatable-requested) / float64(allocatable)
}

// leastUsed is the telemetry-only heuristic: it ranks on measured free CPU and memory
// rather than on requests. It is the BT arm of the protocol, which separates the benefit of
// observing more resources from the benefit of learning, so it must see exactly the
// telemetry a learned model would see.
type leastUsed struct{}

func (leastUsed) Name() string { return LeastUsed }

func (leastUsed) Requires() []string {
	return []string{snapshot.FeatureAllocatable, snapshot.FeatureTelemetry}
}

func (leastUsed) Scores(s *snapshot.Snapshot) (map[string]float64, error) {
	if len(s.Nodes) == 0 {
		return nil, fmt.Errorf("no candidate nodes")
	}
	scores := make(map[string]float64, len(s.Nodes))
	for _, n := range s.Nodes {
		if n.AllocatableMilliCPU <= 0 || n.AllocatableMemoryBytes <= 0 {
			return nil, fmt.Errorf("node %q has no allocatable CPU or memory", n.Name)
		}
		for _, absent := range n.Missing {
			if absent == snapshot.FeatureTelemetry {
				return nil, fmt.Errorf("node %q has no measured usage", n.Name)
			}
		}
		cpu := free(n.AllocatableMilliCPU, n.UsedMilliCPU+s.Pod.RequestedMilliCPU)
		memory := free(n.AllocatableMemoryBytes, n.UsedMemoryBytes+s.Pod.RequestedMemoryBytes)
		scores[n.Name] = (cpu + memory) / 2
	}
	return scores, nil
}
