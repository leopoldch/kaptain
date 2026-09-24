package scoring

import (
	"sort"

	"kaptain/snapshot"
)

// FallbackName runs when something else already failed, so it uses no telemetry of its own.
// A fallback is a decision of the method under test: logged, counted, kept in the results.
const FallbackName = "least-allocated-requests"

// fallbackIncomplete reports the candidates whose allocatable or requested resources are
// missing. The fallback still has to answer -- it runs when something else already failed
// -- but a ranking built partly on zeros must say so rather than pass for a measurement.
func fallbackIncomplete(s *snapshot.Snapshot) []string {
	var blind []string
	for _, n := range s.Nodes {
		for _, feature := range n.Missing {
			if feature == snapshot.FeatureAllocatable || feature == snapshot.FeatureRequested {
				blind = append(blind, n.Name)
				break
			}
		}
	}
	return blind
}

func fallbackNode(s *snapshot.Snapshot) string {
	nodes := append([]snapshot.Node(nil), s.Nodes...)
	sort.Slice(nodes, func(i, j int) bool {
		left, right := nodes[i], nodes[j]
		if free := freeRatio(left, s.Pod); free != freeRatio(right, s.Pod) {
			return free > freeRatio(right, s.Pod)
		}
		if left.AllocatableMilliCPU != right.AllocatableMilliCPU {
			return left.AllocatableMilliCPU > right.AllocatableMilliCPU
		}
		return left.Name < right.Name
	})
	if len(nodes) == 0 {
		return ""
	}
	return nodes[0].Name
}

func fallbackScores(s *snapshot.Snapshot) map[string]float64 {
	chosen := fallbackNode(s)
	scores := make(map[string]float64, len(s.Nodes))
	for _, n := range s.Nodes {
		scores[n.Name] = 0
	}
	if chosen != "" {
		scores[chosen] = 1
	}
	return scores
}

// freeRatio is the mean of the CPU and memory ratios left once the incoming Pod is added,
// which is the same definition as the least-allocated strategy in bridge/strategies. The
// fallback is named after it, so it has to compute it: CPU alone was a different rule under
// the same name.
func freeRatio(n snapshot.Node, pod snapshot.Pod) float64 {
	cpu := free(n.AllocatableMilliCPU, n.RequestedMilliCPU+pod.RequestedMilliCPU)
	memory := free(n.AllocatableMemoryBytes, n.RequestedMemoryBytes+pod.RequestedMemoryBytes)
	return (cpu + memory) / 2
}

func free(allocatable, requested int64) float64 {
	if allocatable <= 0 || requested >= allocatable {
		return 0
	}
	return float64(allocatable-requested) / float64(allocatable)
}
