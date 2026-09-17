package kaptain

import (
	"os"
	"strconv"
	"time"

	"github.com/leopoldch/kaptain/plugin/pkg/telemetry"
)

// Config comes from the environment, exactly like the Python extender, so switching a run
// from one integration to the other changes the deployment and nothing else.
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

func configFromEnv() Config {
	return Config{
		Strategy:        env("KAPTAIN_STRATEGY", defaultStrategy),
		Seed:            envInt("KAPTAIN_SEED", 0),
		DeciderURL:      env("KAPTAIN_DECIDER_URL", ""),
		DeciderTimeout:  envDuration("KAPTAIN_DECIDER_TIMEOUT", defaultDeciderTimeout),
		ReservationTTL:  envDuration("KAPTAIN_RESERVATION_TTL", defaultReservationTTL),
		Telemetry:       env("KAPTAIN_TELEMETRY", telemetry.Disabled),
		TelemetryEvery:  envDuration("KAPTAIN_TELEMETRY_REFRESH", defaultTelemetryEvery),
		TelemetryMaxAge: envDuration("KAPTAIN_TELEMETRY_MAX_AGE", defaultTelemetryMaxAge),
		RunID:           env("RUN_ID", unset),
		PolicyVersion:   env("POLICY_VERSION", unset),
	}
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

func envDuration(key string, fallback time.Duration) time.Duration {
	value, err := time.ParseDuration(os.Getenv(key))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}
