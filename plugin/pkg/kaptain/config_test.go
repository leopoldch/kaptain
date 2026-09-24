package kaptain

import (
	"testing"
	"time"
)

// Accepting only one of "2" and "2s" silently replaced a configured value with the default.
func TestDurationsAcceptBothUnitStyles(t *testing.T) {
	for _, testCase := range []struct {
		value string
		want  time.Duration
	}{
		{"2", 2 * time.Second},
		{"2s", 2 * time.Second},
		{"500ms", 500 * time.Millisecond},
		{"1.5", 1500 * time.Millisecond},
		{"1m30s", 90 * time.Second},
	} {
		t.Setenv("KAPTAIN_TEST_DURATION", testCase.value)
		got, err := envDuration("KAPTAIN_TEST_DURATION", time.Hour)
		if err != nil {
			t.Fatalf("%q: %v", testCase.value, err)
		}
		if got != testCase.want {
			t.Errorf("%q = %s, want %s", testCase.value, got, testCase.want)
		}
	}
}

func TestUnparseableDurationIsRefusedNotDefaulted(t *testing.T) {
	for _, value := range []string{"2x", "abc", "0", "-5s"} {
		t.Setenv("KAPTAIN_TEST_DURATION", value)
		if _, err := envDuration("KAPTAIN_TEST_DURATION", time.Hour); err == nil {
			t.Errorf("%q was accepted", value)
		}
	}
}

func TestEmptyDurationUsesTheDefault(t *testing.T) {
	t.Setenv("KAPTAIN_TEST_DURATION", "")
	got, err := envDuration("KAPTAIN_TEST_DURATION", 7*time.Second)
	if err != nil || got != 7*time.Second {
		t.Fatalf("got (%s, %v), want (7s, nil)", got, err)
	}
}

func TestStartupFailsOnABadDuration(t *testing.T) {
	t.Setenv("KAPTAIN_DECIDER_TIMEOUT", "50")
	if _, err := configFromEnv(); err != nil {
		t.Fatalf("bare seconds rejected: %v", err)
	}

	t.Setenv("KAPTAIN_DECIDER_TIMEOUT", "50 milliseconds")
	if _, err := configFromEnv(); err == nil {
		t.Fatal("an unparseable duration did not fail the configuration")
	}
}
