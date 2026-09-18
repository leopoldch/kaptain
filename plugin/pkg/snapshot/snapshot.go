// Package snapshot holds the decision snapshot shared by the Go plugin and the Python
// extender. Both integrations must see the same fields so that the same strategy reaches
// the same decision; the JSON tags are the wire contract with the external decider.
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

// TaskIDLabel and TaskIDAnnotation carry a workload identity that survives pod
// recreation. The seeded draw keys on it, so the same experiment replays to the same
// placements; a pod UID would change on every recreation and silently break that.
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
	// Two ages, never one. Requested resources and measured usage come from different
	// sources at different rates, and a single mixed age cannot say which one is stale.
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

// Best returns the highest-scoring node, ties broken by name so that both integrations
// and repeated runs agree. Nodes absent from scores are ignored.
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

// TaskID is the stable identity used for seeded decisions: the kaptain.io/task-id label or
// annotation when the runner sets one, else namespace/name, else the UID as a last resort.
// Never the UID alone, which changes whenever the pod is recreated.
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

// Pick chooses one name from a seed deterministically. FNV-1a keeps the rule portable:
// the Python extender implements the identical function, so seeded runs are comparable.
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
