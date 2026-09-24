package kaptain

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/leopoldch/kaptain/plugin/pkg/telemetry"
)

// Config comes from the environment. Durations accept "2s", "500ms" or a bare number of
// seconds, and an unparseable value fails at startup rather than becoming a default.
type Config struct {
	Strategy        string
	Seed            int64
	DeciderURL      string
	DeciderTimeout  time.Duration
	ReservationTTL  time.Duration
	Telemetry       string
	TelemetryEvery  time.Duration
	TelemetryMaxAge time.Duration
	RunID           string
	PolicyVersion   string
}

const (
	defaultStrategy        = "dummy-random"
	defaultDeciderTimeout  = 50 * time.Millisecond
	defaultReservationTTL  = 30 * time.Second
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

	config := Config{
		Strategy:        env("KAPTAIN_STRATEGY", defaultStrategy),
		Seed:            envInt("KAPTAIN_SEED", 0),
		DeciderURL:      env("KAPTAIN_DECIDER_URL", ""),
		DeciderTimeout:  duration("KAPTAIN_DECIDER_TIMEOUT", defaultDeciderTimeout),
		ReservationTTL:  duration("KAPTAIN_RESERVATION_TTL", defaultReservationTTL),
		Telemetry:       env("KAPTAIN_TELEMETRY", telemetry.Disabled),
		TelemetryEvery:  duration("KAPTAIN_TELEMETRY_REFRESH", defaultTelemetryEvery),
		TelemetryMaxAge: duration("KAPTAIN_TELEMETRY_MAX_AGE", defaultTelemetryMaxAge),
		RunID:           env("RUN_ID", unset),
		PolicyVersion:   env("POLICY_VERSION", unset),
	}
	if len(errs) > 0 {
		return config, fmt.Errorf("%s", strings.Join(errs, "; "))
	}
	return config, nil
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envInt(key string, fallback int64) int64 {
	value, err := strconv.ParseInt(os.Getenv(key), 10, 64)
	if err != nil {
		return fallback
	}
	return value
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
