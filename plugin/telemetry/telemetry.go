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
	AgeSeconds  float64
}

type sample struct {
	milliCPU    int64
	memoryBytes int64
	measuredAt  time.Time
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
	MaxAge   time.Duration
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
	api      kubernetes.Interface
	interval time.Duration
	maxAge   time.Duration

	mutex   sync.RWMutex
	samples map[string]sample
}

func newKubelet(ctx context.Context, opts Options) (Source, error) {
	api, err := kubernetes.NewForConfig(opts.Config)
	if err != nil {
		return nil, err
	}
	source := &client{
		api:      api,
		interval: opts.Interval,
		maxAge:   opts.MaxAge,
		samples:  map[string]sample{},
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
	return sample{
		milliCPU:    int64(*cpu.UsageNanoCores / 1_000_000),
		memoryBytes: int64(*memory.WorkingSetBytes),
		measuredAt:  measuredAt,
	}, true
}

func (c *client) Get(node string) (Usage, bool) {
	c.mutex.RLock()
	entry, ok := c.samples[node]
	c.mutex.RUnlock()
	if !ok {
		return Usage{}, false
	}

	age := time.Since(entry.measuredAt)
	if age < 0 {
		age = 0
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
