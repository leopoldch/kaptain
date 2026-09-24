// Package telemetry reads measured node usage. The refresh runs in the background, so a
// decision never calls the API; it reads the last sample and carries its age.
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

// sample dates the values from when the source says they were measured, not from the download.
type sample struct {
	milliCPU    int64
	memoryBytes int64
	measuredAt  time.Time
}

// Source is what the snapshot builder sees; nil means no telemetry, reported as missing.
type Source interface {
	Get(node string) (Usage, bool)
	Name() string
}

// Kubelet is reserved: it can be added without touching the strategies.
const (
	Disabled   = "off"
	MetricsAPI = "metrics-api"
	Kubelet    = "kubelet"
)

// Options configure a collector: refresh interval, and how old a measurement may be.
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

// newMetricsAPI starts a background refresher. A failing refresh leaves the previous sample
// in place until it ages out, so telemetry loss degrades to a fallback rather than to zeros.
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
			// metrics-server serves a cached measurement: dating it from the download here
			// would make a stale value look fresh.
			measuredAt: item.Timestamp.Time,
		}
	}
	c.mutex.Lock()
	defer c.mutex.Unlock()
	c.samples, c.updatedAt = samples, time.Now()
	return nil
}

// Get refuses a sample without a usable timestamp: an age that cannot be known cannot be
// checked against the limit.
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
