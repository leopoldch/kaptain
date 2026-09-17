package telemetry

import (
	"context"
	"testing"
	"time"
)

func source(maxAge time.Duration, samples map[string]sample) *client {
	return &client{maxAge: maxAge, samples: samples, updatedAt: time.Now()}
}

// A fresh download of an old measurement must not look fresh: metrics-server serves a
// cached sample, so the age has to come from the sample's own timestamp.
func TestAgeComesFromTheMeasurementNotTheDownload(t *testing.T) {
	c := source(30*time.Second, map[string]sample{
		"fresh": {milliCPU: 500, memoryBytes: 1024, measuredAt: time.Now().Add(-2 * time.Second)},
		"stale": {milliCPU: 500, memoryBytes: 1024, measuredAt: time.Now().Add(-5 * time.Minute)},
	})

	usage, ok := c.Get("fresh")
	if !ok {
		t.Fatal("a recent measurement was refused")
	}
	if usage.AgeSeconds < 1.5 || usage.AgeSeconds > 3 {
		t.Fatalf("age %.2fs, want about 2s", usage.AgeSeconds)
	}

	if _, ok := c.Get("stale"); ok {
		t.Fatal("a five-minute-old measurement was served as fresh")
	}
}

func TestSampleWithoutTimestampIsRefused(t *testing.T) {
	c := source(30*time.Second, map[string]sample{"unknown": {milliCPU: 500}})
	if _, ok := c.Get("unknown"); ok {
		t.Fatal("a sample of unknown age was accepted")
	}
}

func TestUnknownNodeIsRefused(t *testing.T) {
	c := source(30*time.Second, map[string]sample{})
	if _, ok := c.Get("absent"); ok {
		t.Fatal("an unknown node returned usage")
	}
}

func TestClockSkewDoesNotProduceANegativeAge(t *testing.T) {
	c := source(30*time.Second, map[string]sample{
		"ahead": {milliCPU: 1, measuredAt: time.Now().Add(5 * time.Second)},
	})
	usage, ok := c.Get("ahead")
	if !ok {
		t.Fatal("a sample timestamped slightly in the future was refused")
	}
	if usage.AgeSeconds != 0 {
		t.Fatalf("age %.2fs, want 0", usage.AgeSeconds)
	}
}

func TestOpenSelectsTheCollector(t *testing.T) {
	for _, collector := range []string{"", Disabled} {
		source, err := Open(context.Background(), collector, Options{})
		if err != nil || source != nil {
			t.Fatalf("Open(%q) = (%v, %v), want (nil, nil)", collector, source, err)
		}
	}

	// Reserved, and refused explicitly rather than silently falling back to metrics-server:
	// the point of the interface is to make this comparable later, not to pretend it exists.
	if _, err := Open(context.Background(), Kubelet, Options{}); err == nil {
		t.Fatal("the kubelet collector was accepted although it is not implemented")
	}

	if _, err := Open(context.Background(), "prometheus", Options{}); err == nil {
		t.Fatal("an unknown collector was accepted")
	}
}

func TestCollectorNamesItself(t *testing.T) {
	c := source(time.Second, map[string]sample{})
	if c.Name() != MetricsAPI {
		t.Fatalf("Name() = %q, want %q", c.Name(), MetricsAPI)
	}
}
