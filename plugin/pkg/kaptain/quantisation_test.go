package kaptain

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"sort"
	"testing"

	extenderv1 "k8s.io/kube-scheduler/extender/v1"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
	"github.com/leopoldch/kaptain/plugin/pkg/strategies"
)

// The Python decider walks the same table, so a divergence breaks one of the two suites.
func TestQuantisationMatchesTheDecider(t *testing.T) {
	raw, err := os.ReadFile("../../../testdata/quantisation.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []struct {
			Raw      map[string]float64 `json:"raw"`
			Expected map[string]int64   `json:"expected"`
			Winner   string             `json:"winner"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared quantisation table is empty")
	}

	for _, testCase := range table.Cases {
		lowest, highest := math.Inf(1), math.Inf(-1)
		for _, value := range testCase.Raw {
			lowest, highest = math.Min(lowest, value), math.Max(highest, value)
		}

		names := make([]string, 0, len(testCase.Raw))
		for name := range testCase.Raw {
			names = append(names, name)
		}
		sort.Strings(names)

		best, bestScore := "", int64(-1)
		for _, name := range names {
			got := QuantizeRaw(testCase.Raw[name], lowest, highest)
			if want := testCase.Expected[name]; got != want {
				t.Errorf("QuantizeRaw(%v) for %q = %d, want %d", testCase.Raw, name, got, want)
			}
			if got > bestScore {
				best, bestScore = name, got
			}
		}
		if best != testCase.Winner {
			t.Errorf("winner after normalisation for %v is %q, want %q", testCase.Raw, best, testCase.Winner)
		}
		if bestScore != extenderv1.MaxExtenderPriority {
			t.Errorf("top score for %v is %d, want %d", testCase.Raw, bestScore, extenderv1.MaxExtenderPriority)
		}
	}
}

// TestFixturesNormaliseAsExpected checks the winner after normalisation, not only the raw
// argmax: the placement follows the normalised scores.
func TestFixturesNormaliseAsExpected(t *testing.T) {
	paths, _ := filepath.Glob("../../../testdata/snapshots/*.json")
	if len(paths) == 0 {
		t.Fatal("no fixtures found")
	}
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read %s: %v", path, err)
		}
		var decoded struct {
			Snapshot           snapshot.Snapshot           `json:"snapshot"`
			Expected           map[string]string           `json:"expected"`
			ExpectedNormalized map[string]map[string]int64 `json:"expected_normalized"`
		}
		if err := json.Unmarshal(raw, &decoded); err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}

		for name, wanted := range decoded.ExpectedNormalized {
			strategy, err := strategies.Get(name)
			if err != nil {
				t.Fatalf("Get(%q): %v", name, err)
			}
			current := decoded.Snapshot
			scores, err := strategy.Scores(&current)
			if err != nil {
				t.Fatalf("%s on %s: %v", name, path, err)
			}

			lowest, highest := math.Inf(1), math.Inf(-1)
			for _, value := range scores {
				lowest, highest = math.Min(lowest, value), math.Max(highest, value)
			}
			for node, want := range wanted {
				if got := QuantizeRaw(scores[node], lowest, highest); got != want {
					t.Errorf("%s on %s: node %q normalised to %d, want %d",
						name, filepath.Base(path), node, got, want)
				}
			}
			if want := decoded.Expected[name]; wanted[want] != extenderv1.MaxExtenderPriority {
				t.Errorf("%s on %s: intended node %q is not top after normalisation",
					name, filepath.Base(path), want)
			}
		}
	}
}
