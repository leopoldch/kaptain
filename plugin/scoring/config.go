package scoring

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"kaptain/telemetry"
)

// Config comes from the environment. Durations accept "2s", "500ms" or a bare number of
// seconds, and an unparseable value fails at startup rather than becoming a default.
type Config struct {
	Strategy        string
	Seed            int64
	DeciderURL      string
	DeciderTimeout  time.Duration
	Telemetry       string
	TelemetryEvery  time.Duration
	TelemetryMaxAge time.Duration
	RunID           string
	PolicyVersion   string

	// InstanceID distinguishes one scheduler process from the next. RUN_ID survives a
	// restart and the decision counter does not, so without this two processes of the same
	// run hand out the same decision ids.
	InstanceID string
}

const (
	defaultStrategy        = "dummy-random"
	defaultDeciderTimeout  = 50 * time.Millisecond
	defaultTelemetryEvery  = 2 * time.Second
	defaultTelemetryMaxAge = 30 * time.Second
	unset                  = "unset"
)

func configFromEnv() (Config, error) {
	var errs []string
	duration := func(key string, fallback time.Duration) time.Duration {
		value, err := envDuration(key, fallback)
		if err != nil {
			errs = append(errs, err.Error())
		}
		return value
	}

	integer := func(key string, fallback int64) int64 {
		value, err := envInt(key, fallback)
		if err != nil {
			errs = append(errs, err.Error())
		}
		return value
	}

	config := Config{
		Strategy:        env("KAPTAIN_STRATEGY", defaultStrategy),
		Seed:            integer("KAPTAIN_SEED", 0),
		DeciderURL:      env("KAPTAIN_DECIDER_URL", ""),
		DeciderTimeout:  duration("KAPTAIN_DECIDER_TIMEOUT", defaultDeciderTimeout),
		Telemetry:       env("KAPTAIN_TELEMETRY", telemetry.Kubelet),
		TelemetryEvery:  duration("KAPTAIN_TELEMETRY_REFRESH", defaultTelemetryEvery),
		TelemetryMaxAge: duration("KAPTAIN_TELEMETRY_MAX_AGE", defaultTelemetryMaxAge),
		RunID:           env("RUN_ID", unset),
		PolicyVersion:   env("POLICY_VERSION", unset),
		InstanceID:      instanceID(),
	}
	if len(errs) > 0 {
		return config, fmt.Errorf("%s", strings.Join(errs, "; "))
	}
	return config, nil
}

// instanceID names the process, not the Pod: a container restart keeps POD_NAME, so the
// Pod name alone lets a second process reuse the first one's decision ids. The nonce is
// what makes them unique; POD_NAME is there so a human can find the process afterwards.
func instanceID() string {
	var raw [8]byte
	nonce := unset
	if _, err := rand.Read(raw[:]); err == nil {
		nonce = hex.EncodeToString(raw[:])
	}
	if name := os.Getenv("POD_NAME"); name != "" {
		return name + "-" + nonce
	}
	return nonce
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

// envInt refuses what it cannot parse. KAPTAIN_SEED=abc silently becoming 0 would give a
// run a seed it never chose, and every seeded draw in it would be unexplainable.
func envInt(key string, fallback int64) (int64, error) {
	text := strings.TrimSpace(os.Getenv(key))
	if text == "" {
		return fallback, nil
	}
	value, err := strconv.ParseInt(text, 10, 64)
	if err != nil {
		return fallback, fmt.Errorf("%s=%q is not an integer", key, text)
	}
	return value, nil
}

// envDuration refuses what it cannot parse, so a typo cannot pass for a configured value.
func envDuration(key string, fallback time.Duration) (time.Duration, error) {
	text := strings.TrimSpace(os.Getenv(key))
	if text == "" {
		return fallback, nil
	}

	if value, err := time.ParseDuration(text); err == nil {
		if value <= 0 {
			return fallback, fmt.Errorf("%s=%q must be positive", key, text)
		}
		return value, nil
	}

	seconds, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return fallback, fmt.Errorf("%s=%q is neither a duration (\"2s\") nor a number of seconds (\"2\")", key, text)
	}
	if seconds <= 0 {
		return fallback, fmt.Errorf("%s=%q must be positive", key, text)
	}
	return time.Duration(seconds * float64(time.Second)), nil
}
