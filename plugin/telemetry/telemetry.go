// Package telemetry reads node usage from each kubelet. Refresh runs in the background, so
// a scheduling decision only reads the last sample and carries its age.
package telemetry

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	restclient "k8s.io/client-go/rest"
	"k8s.io/klog/v2"
	statsapi "k8s.io/kubelet/pkg/apis/stats/v1alpha1"
)

type Usage struct {
	MilliCPU    int64
	MemoryBytes int64

	// AgeSeconds is how long ago this sample reached us, on our clock. It is the cache age,
	// not the age of the measurement: the kubelet may keep returning the same numbers.
	AgeSeconds float64

	// SourceTimestampDeltaSeconds is our receipt time minus the kubelet's timestamp for the
	// same sample. It is NOT clock skew: it also contains how old the sample already was
	// inside the kubelet, the round trip and the decoding. Two perfectly synchronised
	// machines still show a tenth of a second here. Report it as what it is.
	SourceTimestampDeltaSeconds float64

	// SourceStaleSeconds is how long the kubelet has been returning a measurement with the
	// same timestamp. A frozen stats pipeline answers happily forever, and cache age alone
	// would call every one of those answers fresh.
	SourceStaleSeconds float64
}

type sample struct {
	milliCPU    int64
	memoryBytes int64

	// measuredAt is the kubelet's own clock; receivedAt is ours. Expiry uses receivedAt,
	// because the difference between two machines' clocks is not an age: a node running
	// ahead would look permanently fresh, one running behind would expire while current.
	// measuredAt is kept because the gap between the two is what reveals the skew.
	measuredAt time.Time
	receivedAt time.Time

	// firstSeenAt is when this measuredAt was first observed, so an unchanging kubelet
	// timestamp becomes visible instead of being refreshed away on every poll.
	firstSeenAt time.Time
}

type Source interface {
	Get(node string) (Usage, bool)
	Name() string
}

const (
	Disabled = "off"
	Kubelet  = "kubelet"
)

type Options struct {
	Config   *restclient.Config
	Interval time.Duration

	// MaxAge bounds how long ago we received a sample. MaxSourceStale bounds how long the
	// kubelet has been repeating the same measurement timestamp: a frozen stats pipeline
	// keeps answering, so every poll refreshes MaxAge and only this catches it. They are
	// separate because an unchanged timestamp for a few seconds is perfectly normal.
	MaxAge         time.Duration
	MaxSourceStale time.Duration
}

func Open(ctx context.Context, collector string, opts Options) (Source, error) {
	switch collector {
	case "", Disabled:
		return nil, nil
	case Kubelet:
		return newKubelet(ctx, opts)
	default:
		return nil, fmt.Errorf("unknown telemetry collector %q, known: %s, %s", collector, Disabled, Kubelet)
	}
}

type client struct {
	api            kubernetes.Interface
	interval       time.Duration
	maxAge         time.Duration
	maxSourceStale time.Duration

	mutex   sync.RWMutex
	samples map[string]sample
}

func newKubelet(ctx context.Context, opts Options) (Source, error) {
	api, err := kubernetes.NewForConfig(opts.Config)
	if err != nil {
		return nil, err
	}
	source := &client{
		api:            api,
		interval:       opts.Interval,
		maxAge:         opts.MaxAge,
		maxSourceStale: opts.MaxSourceStale,
		samples:        map[string]sample{},
	}
	if err := source.refresh(ctx); err != nil {
		klog.ErrorS(err, "kaptain telemetry: first kubelet refresh failed, starting without samples")
	}
	go source.loop(ctx)
	return source, nil
}

func (c *client) Name() string { return Kubelet }

func (c *client) loop(ctx context.Context) {
	ticker := time.NewTicker(c.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := c.refresh(ctx); err != nil {
				klog.V(2).ErrorS(err, "kaptain telemetry: kubelet refresh failed")
			}
		}
	}
}

func (c *client) refresh(ctx context.Context) error {
	nodes, err := c.api.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
	if err != nil {
		return fmt.Errorf("list nodes: %w", err)
	}

	previous := make(map[string]sample)
	c.mutex.RLock()
	for name, entry := range c.samples {
		previous[name] = entry
	}
	c.mutex.RUnlock()
	updated := make(map[string]sample, len(nodes.Items))

	for _, node := range nodes.Items {
		var summary statsapi.Summary
		body, err := c.api.CoreV1().RESTClient().Get().
			Resource("nodes").Name(node.Name).SubResource("proxy").
			Suffix("stats/summary").Timeout(c.interval).Do(ctx).Raw()
		if err != nil {
			klog.V(2).ErrorS(err, "kaptain telemetry: kubelet summary request failed", "node", node.Name)
			if entry, ok := previous[node.Name]; ok {
				updated[node.Name] = entry
			}
			continue
		}
		if err := json.Unmarshal(body, &summary); err != nil {
			klog.V(2).ErrorS(err, "kaptain telemetry: decode kubelet summary failed", "node", node.Name)
			if entry, ok := previous[node.Name]; ok {
				updated[node.Name] = entry
			}
			continue
		}

		if entry, ok := sampleFromSummary(summary); ok {
			// A kubelet that keeps answering with the same measurement timestamp is not
			// producing fresh numbers. Carry the instant that timestamp first appeared, so
			// the staleness of the source survives a poll that only refreshed our clock.
			if before, seen := previous[node.Name]; seen && before.measuredAt.Equal(entry.measuredAt) {
				entry.firstSeenAt = before.firstSeenAt
			}
			updated[node.Name] = entry
		} else {
			klog.V(2).InfoS("kaptain telemetry: kubelet summary lacks CPU or memory usage", "node", node.Name)
			if entry, ok := previous[node.Name]; ok {
				updated[node.Name] = entry
			}
		}
	}

	c.mutex.Lock()
	c.samples = updated
	c.mutex.Unlock()
	return nil
}

func sampleFromSummary(summary statsapi.Summary) (sample, bool) {
	cpu, memory := summary.Node.CPU, summary.Node.Memory
	if cpu == nil || memory == nil || cpu.UsageNanoCores == nil || memory.WorkingSetBytes == nil ||
		cpu.Time.IsZero() || memory.Time.IsZero() {
		return sample{}, false
	}

	measuredAt := cpu.Time.Time
	if memory.Time.Time.Before(measuredAt) {
		measuredAt = memory.Time.Time
	}
	now := time.Now()
	return sample{
		milliCPU:    int64(*cpu.UsageNanoCores / 1_000_000),
		memoryBytes: int64(*memory.WorkingSetBytes),
		measuredAt:  measuredAt,
		receivedAt:  now,
		firstSeenAt: now,
	}, true
}

func (c *client) Get(node string) (Usage, bool) {
	c.mutex.RLock()
	entry, ok := c.samples[node]
	c.mutex.RUnlock()
	if !ok {
		return Usage{}, false
	}

	age := time.Since(entry.receivedAt)
	if age < 0 {
		age = 0
	}
	sourceStale := entry.receivedAt.Sub(entry.firstSeenAt)
	if sourceStale < 0 {
		sourceStale = 0
	}
	// Refused on either count: a sample we have not refreshed, or one the kubelet has
	// stopped moving. Reporting the second without acting on it would leave least-used
	// ranking on a measurement frozen an hour ago.
	if age > c.maxAge || (c.maxSourceStale > 0 && sourceStale > c.maxSourceStale) {
		return Usage{}, false
	}
	return Usage{
		MilliCPU:                    entry.milliCPU,
		MemoryBytes:                 entry.memoryBytes,
		AgeSeconds:                  age.Seconds(),
		SourceTimestampDeltaSeconds: entry.receivedAt.Sub(entry.measuredAt).Seconds(),
		SourceStaleSeconds:          sourceStale.Seconds(),
	}, true
}
