// Package telemetry reads measured node usage from the metrics API.
//
// Requested resources say what pods asked for; they say nothing about what is actually
// used. Every strategy in the corpus that claims to be resource-aware needs the measured
// value, and so does the BT arm of our protocol, which isolates the benefit of observing
// more resources from the benefit of learning.
//
// The refresh runs in the background: a scheduling decision never calls the API. It reads
// the last sample and carries its age, so a stale value is visible instead of silent.
package telemetry

import (
	"context"
	"sync"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	restclient "k8s.io/client-go/rest"
	"k8s.io/klog/v2"
	metricsclient "k8s.io/metrics/pkg/client/clientset/versioned"
)

// Usage is one node's measured consumption, with the age of the measurement itself.
type Usage struct {
	MilliCPU    int64
	MemoryBytes int64
	AgeSeconds  float64
}

// sample is what the refresh stores: the values plus the instant the metrics source says
// they were measured, which is not the instant we downloaded them.
type sample struct {
	milliCPU    int64
	memoryBytes int64
	measuredAt  time.Time
}

// Source is what a strategy sees. A nil Source means no telemetry at all, which the
// snapshot reports as a missing feature.
type Source interface {
	Get(node string) (Usage, bool)
}

type client struct {
	api      metricsclient.Interface
	interval time.Duration
	maxAge   time.Duration

	mutex     sync.RWMutex
	samples   map[string]sample
	updatedAt time.Time
}

// New starts a background refresher. It returns an error only if the client cannot be
// built; a failing refresh is logged and leaves the previous sample in place until it
// ages out, so telemetry loss degrades to an explicit fallback rather than to zeros.
func New(ctx context.Context, config *restclient.Config, interval, maxAge time.Duration) (Source, error) {
	api, err := metricsclient.NewForConfig(config)
	if err != nil {
		return nil, err
	}
	source := &client{api: api, interval: interval, maxAge: maxAge, samples: map[string]sample{}}
	if err := source.refresh(ctx); err != nil {
		klog.ErrorS(err, "kaptain telemetry: first refresh failed, starting without a sample")
	}
	go source.loop(ctx)
	return source, nil
}

func (c *client) loop(ctx context.Context) {
	ticker := time.NewTicker(c.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := c.refresh(ctx); err != nil {
				klog.V(2).ErrorS(err, "kaptain telemetry: refresh failed")
			}
		}
	}
}

func (c *client) refresh(ctx context.Context) error {
	listed, err := c.api.MetricsV1beta1().NodeMetricses().List(ctx, metav1.ListOptions{})
	if err != nil {
		return err
	}
	samples := make(map[string]sample, len(listed.Items))
	for _, item := range listed.Items {
		cpu := item.Usage[v1.ResourceCPU]
		memory := item.Usage[v1.ResourceMemory]
		samples[item.Name] = sample{
			milliCPU:    cpu.MilliValue(),
			memoryBytes: memory.Value(),
			// The API reports when the sample was taken. metrics-server serves a cached
			// measurement, so dating it from this download would make a stale value look
			// fresh and let a strategy rank on old numbers.
			measuredAt: item.Timestamp.Time,
		}
	}
	c.mutex.Lock()
	defer c.mutex.Unlock()
	c.samples, c.updatedAt = samples, time.Now()
	return nil
}

// Get returns the node's usage and the age of that measurement. A sample without a usable
// timestamp is refused: an age that cannot be known cannot be checked against the limit.
func (c *client) Get(node string) (Usage, bool) {
	c.mutex.RLock()
	entry, ok := c.samples[node]
	c.mutex.RUnlock()
	if !ok || entry.measuredAt.IsZero() {
		return Usage{}, false
	}

	age := time.Since(entry.measuredAt)
	if age < 0 {
		age = 0 // clock skew between us and the metrics source
	}
	if age > c.maxAge {
		return Usage{}, false
	}
	return Usage{
		MilliCPU:    entry.milliCPU,
		MemoryBytes: entry.memoryBytes,
		AgeSeconds:  age.Seconds(),
	}, true
}
