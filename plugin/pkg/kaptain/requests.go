package kaptain

import (
	v1 "k8s.io/api/core/v1"
	resourcehelper "k8s.io/kubernetes/pkg/api/v1/resource"
)

// podRequests uses kube-scheduler's own helper: sidecars add up, ordinary init containers
// contribute a maximum, and pod overhead is added last. Writing that by hand is a mistake.
func podRequests(pod *v1.Pod) (milliCPU, memoryBytes int64) {
	requests := resourcehelper.PodRequests(pod, resourcehelper.PodResourcesOptions{})
	cpu := requests[v1.ResourceCPU]
	memory := requests[v1.ResourceMemory]
	return cpu.MilliValue(), memory.Value()
}
