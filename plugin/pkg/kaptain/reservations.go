package kaptain

import (
	"sync"
	"time"

	v1 "k8s.io/api/core/v1"
)

// reservations bridges Reserve and the moment the pod appears in the scheduler cache, so
// two decisions taken close together do not both see the node as free. Entries expire.
type reservations struct {
	mutex sync.Mutex
	ttl   time.Duration
	byUID map[string]reservation
	now   func() time.Time
}

type reservation struct {
	node                 string
	requestedMilliCPU    int64
	requestedMemoryBytes int64
	at                   time.Time
}

func newReservations(ttl time.Duration) *reservations {
	return &reservations{ttl: ttl, byUID: map[string]reservation{}, now: time.Now}
}

func (r *reservations) add(pod *v1.Pod, nodeName string) {
	cpu, memory := podRequests(pod)
	r.mutex.Lock()
	defer r.mutex.Unlock()
	r.byUID[string(pod.UID)] = reservation{
		node:                 nodeName,
		requestedMilliCPU:    cpu,
		requestedMemoryBytes: memory,
		at:                   r.now(),
	}
}

func (r *reservations) remove(pod *v1.Pod) {
	r.mutex.Lock()
	defer r.mutex.Unlock()
	delete(r.byUID, string(pod.UID))
}

// pending skips expired entries and pods the cache already counts, to avoid counting twice.
func (r *reservations) pending(counted map[string]bool) map[string]reservation {
	r.mutex.Lock()
	defer r.mutex.Unlock()
	now := r.now()
	out := map[string]reservation{}
	for uid, entry := range r.byUID {
		if now.Sub(entry.at) > r.ttl {
			delete(r.byUID, uid)
			continue
		}
		if counted[uid] {
			delete(r.byUID, uid)
			continue
		}
		out[uid] = entry
	}
	return out
}
