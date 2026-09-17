package kaptain

import (
	v1 "k8s.io/api/core/v1"
	"k8s.io/kubernetes/pkg/scheduler/framework"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
)

// buildSnapshot turns the candidates Kubernetes already filtered into the snapshot shared
// with the extender. It reads the scheduler cache only: no API call, no Prometheus query
// on the critical path. A value that cannot be read is reported as missing, never as zero.
func (p *Plugin) buildSnapshot(pod *v1.Pod, nodes []*framework.NodeInfo) *snapshot.Snapshot {
	podCPU, podMemory := podRequests(pod)
	counted := map[string]bool{}
	for _, node := range nodes {
		for _, podInfo := range node.Pods {
			if podInfo.Pod != nil {
				counted[string(podInfo.Pod.UID)] = true
			}
		}
	}
	pending := p.reservations.pending(counted)

	out := &snapshot.Snapshot{
		RunID:         p.config.RunID,
		PolicyVersion: p.config.PolicyVersion,
		Integration:   Integration,
		Strategy:      p.strategyName(),
		Seed:          p.config.Seed,
		Pod: snapshot.Pod{
			UID:                  string(pod.UID),
			TaskID:               snapshot.TaskID(string(pod.UID), pod.Namespace, pod.Name, pod.Labels, pod.Annotations),
			Namespace:            pod.Namespace,
			Name:                 pod.Name,
			RequestedMilliCPU:    podCPU,
			RequestedMemoryBytes: podMemory,
			Labels:               pod.Labels,
		},
		Nodes: make([]snapshot.Node, 0, len(nodes)),
	}

	for _, node := range nodes {
		out.Nodes = append(out.Nodes, p.nodeSnapshot(node, pending))
	}
	return out
}

func (p *Plugin) nodeSnapshot(node *framework.NodeInfo, pending map[string]reservation) snapshot.Node {
	entry := snapshot.Node{Name: node.GetName(), Pods: len(node.Pods)}

	if node.Allocatable != nil {
		entry.AllocatableMilliCPU = node.Allocatable.MilliCPU
		entry.AllocatableMemoryBytes = node.Allocatable.Memory
	} else {
		entry.Missing = append(entry.Missing, snapshot.FeatureAllocatable)
		missingFeaturesTotal.WithLabelValues(snapshot.FeatureAllocatable).Inc()
	}

	if node.Requested != nil {
		entry.RequestedMilliCPU = node.Requested.MilliCPU
		entry.RequestedMemoryBytes = node.Requested.Memory
	} else {
		entry.Missing = append(entry.Missing, snapshot.FeatureRequested)
		missingFeaturesTotal.WithLabelValues(snapshot.FeatureRequested).Inc()
	}

	for _, held := range pending {
		if held.node == entry.Name {
			entry.RequestedMilliCPU += held.requestedMilliCPU
			entry.RequestedMemoryBytes += held.requestedMemoryBytes
			entry.Pods++
		}
	}

	if p.telemetry != nil {
		if usage, ok := p.telemetry.Get(entry.Name); ok {
			entry.UsedMilliCPU = usage.MilliCPU
			entry.UsedMemoryBytes = usage.MemoryBytes
			entry.MetricsAgeSeconds = usage.AgeSeconds
		} else {
			entry.Missing = append(entry.Missing, snapshot.FeatureTelemetry)
			missingFeaturesTotal.WithLabelValues(snapshot.FeatureTelemetry).Inc()
		}
	} else {
		entry.Missing = append(entry.Missing, snapshot.FeatureTelemetry)
		missingFeaturesTotal.WithLabelValues(snapshot.FeatureTelemetry).Inc()
	}

	if candidate := node.Node(); candidate != nil {
		entry.Ready = nodeReady(candidate)
	}
	return entry
}

func nodeReady(node *v1.Node) bool {
	for _, condition := range node.Status.Conditions {
		if condition.Type == v1.NodeReady {
			return condition.Status == v1.ConditionTrue
		}
	}
	return false
}
