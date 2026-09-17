// Package telemetry reads measured node usage.
//
// Requested resources say what pods asked for; they say nothing about what is actually
// used. Every strategy in the corpus that claims to be resource-aware needs the measured
// value, and so does the BT arm of our protocol, which isolates the benefit of observing
// more resources from the benefit of learning.
//
// The refresh runs in the background: a scheduling decision never calls the API. It reads
// the last sample and carries its age, so a stale value is visible instead of silent.
//
// The collector behind Source is deliberately replaceable. Today it goes through
// metrics-server:
//
//	kubelet -> metrics-server -> metrics.k8s.io -> this cache -> scheduler
//
// A plugin running inside the scheduler could instead read the kubelet summary/resource
// endpoints directly, which would drop an intermediate dependency, lower staleness and
// expose more than CPU and memory. Strategies must not have to change when that happens, so
// they only ever see Source; Open picks the collector from configuration, and the collector
// names itself so results can record which one produced the numbers.
package telemetry

import (
	"context"
	"fmt"
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

// Source is what the snapshot builder sees. A nil Source means no telemetry at all, which
// the snapshot then reports as a missing feature.
type Source interface {
	// Get returns the node's usage and the age of that measurement.
	Get(node string) (Usage, bool)
	// Name identifies the collector, for logs, metric labels and run records.
	Name() string
}

// Collectors. Kubelet is reserved: the interface exists so it can be added and compared
// against MetricsAPI without touching the strategies.
const (
	Disabled   = "off"
	MetricsAPI = "metrics-api"
	Kubelet    = "kubelet"
)

// Options configure a collector. Interval is how often it refreshes, MaxAge how old a
// measurement may be before it is refused.
type Options struct {
	Config   *restclient.Config
	Interval time.Duration
	MaxAge   time.Duration
}

// Open returns the configured collector, or nil for no telemetry at all.
func Open(ctx context.Context, collector string, opts Options) (Source, error) {
	switch collector {
	case "", Disabled:
		return nil, nil
	case MetricsAPI:
		return newMetricsAPI(ctx, opts)
	case Kubelet:
		return nil, fmt.Errorf("telemetry collector %q is not implemented yet; it is reserved "+
			"for reading the kubelet summary endpoints directly, without metrics-server", collector)
	default:
		return nil, fmt.Errorf("unknown telemetry collector %q, known: %s, %s (%s reserved)",
			collector, Disabled, MetricsAPI, Kubelet)
	}
}

type client struct {
	api      metricsclient.Interface
	interval time.Duration
	maxAge   time.Duration

	mutex     sync.RWMutex
	samples   map[string]sample
	updatedAt time.Time
}

// newMetricsAPI starts a background refresher against metrics.k8s.io. It returns an error
// only if the client cannot be built; a failing refresh is logged and leaves the previous
// sample in place until it ages out, so telemetry loss degrades to an explicit fallback
// rather than to zeros.
func newMetricsAPI(ctx context.Context, opts Options) (Source, error) {
	api, err := metricsclient.NewForConfig(opts.Config)
	if err != nil {
		return nil, err
	}
	source := &client{api: api, interval: opts.Interval, maxAge: opts.MaxAge, samples: map[string]sample{}}
	if err := source.refresh(ctx); err != nil {
		klog.ErrorS(err, "kaptain telemetry: first refresh failed, starting without a sample",
			"collector", source.Name())
	}
	go source.loop(ctx)
	return source, nil
}

func (c *client) Name() string { return MetricsAPI }

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
