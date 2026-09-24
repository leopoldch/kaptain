// Package snapshot holds the decision snapshot shared with the Python decider. The JSON
// tags are the wire contract.
package snapshot

import (
	"hash/fnv"
	"sort"
	"strconv"
)

// Feature groups reported as missing, so a gap is visible instead of read as a zero.
const (
	FeatureAllocatable = "allocatable"
	FeatureRequested   = "requested"
	FeatureTelemetry   = "telemetry"
)

// A workload identity that survives pod recreation, which the seeded draw keys on.
const (
	TaskIDLabel      = "kaptain.io/task-id"
	TaskIDAnnotation = "kaptain.io/task-id"
)

type Pod struct {
	UID                  string            `json:"uid"`
	TaskID               string            `json:"task_id"`
	Namespace            string            `json:"namespace"`
	Name                 string            `json:"name"`
	RequestedMilliCPU    int64             `json:"requested_millicpu"`
	RequestedMemoryBytes int64             `json:"requested_memory_bytes"`
	Labels               map[string]string `json:"labels,omitempty"`
}

type Node struct {
	Name                   string `json:"name"`
	AllocatableMilliCPU    int64  `json:"allocatable_millicpu"`
	AllocatableMemoryBytes int64  `json:"allocatable_memory_bytes"`
	RequestedMilliCPU      int64  `json:"requested_millicpu"`
	RequestedMemoryBytes   int64  `json:"requested_memory_bytes"`
	UsedMilliCPU           int64  `json:"used_millicpu"`
	UsedMemoryBytes        int64  `json:"used_memory_bytes"`
	Pods                   int    `json:"pods"`
	Ready                  bool   `json:"ready"`
	// Two ages, never one: a single mixed age cannot say which input was stale.
	RequestsAgeSeconds  float64  `json:"requests_age_seconds,omitempty"`
	TelemetryAgeSeconds float64  `json:"telemetry_age_seconds,omitempty"`
	Missing             []string `json:"missing,omitempty"`
}

type Snapshot struct {
	RunID         string `json:"run_id"`
	PolicyVersion string `json:"policy_version"`
	Integration   string `json:"integration"`
	Strategy      string `json:"strategy"`
	Seed          int64  `json:"seed"`
	Pod           Pod    `json:"pod"`
	Nodes         []Node `json:"nodes"`
}

func (s *Snapshot) Names() []string {
	names := make([]string, 0, len(s.Nodes))
	for _, n := range s.Nodes {
		names = append(names, n.Name)
	}
	sort.Strings(names)
	return names
}

func (s *Snapshot) Has(name string) bool {
	for _, n := range s.Nodes {
		if n.Name == name {
			return true
		}
	}
	return false
}

// Best returns the highest-scoring node, ties broken by name so both sides agree.
func Best(names []string, scores map[string]float64) string {
	sorted := append([]string(nil), names...)
	sort.Strings(sorted)
	best, bestScore := "", 0.0
	for _, name := range sorted {
		score, ok := scores[name]
		if !ok {
			continue
		}
		if best == "" || score > bestScore {
			best, bestScore = name, score
		}
	}
	return best
}

// TaskID: the kaptain.io/task-id label or annotation, else namespace/name, else the UID.
func TaskID(uid, namespace, name string, labels, annotations map[string]string) string {
	if value := labels[TaskIDLabel]; value != "" {
		return value
	}
	if value := annotations[TaskIDAnnotation]; value != "" {
		return value
	}
	if name != "" {
		return namespace + "/" + name
	}
	return uid
}

// Pick is a deterministic seeded draw. FNV-1a keeps it identical to the Python side.
func Pick(names []string, seed int64, key string) string {
	if len(names) == 0 {
		return ""
	}
	sorted := append([]string(nil), names...)
	sort.Strings(sorted)
	h := fnv.New64a()
	h.Write([]byte(strconv.FormatInt(seed, 10)))
	h.Write([]byte(":"))
	h.Write([]byte(key))
	return sorted[h.Sum64()%uint64(len(sorted))]
}
