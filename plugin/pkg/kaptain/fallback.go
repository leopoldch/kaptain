package kaptain

import (
	"sort"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
)

// FallbackName runs when something else already failed, so it uses no telemetry of its own.
// A fallback is a decision of the method under test: logged, counted, kept in the results.
const FallbackName = "least-allocated-requests"

func fallbackNode(s *snapshot.Snapshot) string {
	nodes := append([]snapshot.Node(nil), s.Nodes...)
	sort.Slice(nodes, func(i, j int) bool {
		left, right := nodes[i], nodes[j]
		if free := freeRatio(left); free != freeRatio(right) {
			return free > freeRatio(right)
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

func freeRatio(n snapshot.Node) float64 {
	if n.AllocatableMilliCPU <= 0 {
		return 0
	}
	free := n.AllocatableMilliCPU - n.RequestedMilliCPU
	if free <= 0 {
		return 0
	}
	return float64(free) / float64(n.AllocatableMilliCPU)
}
