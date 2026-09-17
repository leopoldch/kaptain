package kaptain

import (
	v1 "k8s.io/api/core/v1"
	resourcehelper "k8s.io/kubernetes/pkg/api/v1/resource"
)

// podRequests returns the pod's effective requests, using the same helper kube-scheduler
// uses. Writing this rule by hand is a mistake: it has to account for restartable init
// containers (sidecars), which add up, for ordinary init containers, which contribute a
// maximum rather than a sum, and for pod overhead. `bridge/snapshot.py` ports this
// algorithm, and testdata/pod-requests.json pins both against the same cases.
func podRequests(pod *v1.Pod) (milliCPU, memoryBytes int64) {
	requests := resourcehelper.PodRequests(pod, resourcehelper.PodResourcesOptions{})
	cpu := requests[v1.ResourceCPU]
	memory := requests[v1.ResourceMemory]
	return cpu.MilliValue(), memory.Value()
}
