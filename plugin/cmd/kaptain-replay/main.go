// kaptain-replay scores a recorded snapshot with the plugin's own strategies and
// quantisation, outside kube-scheduler.
//
// It exists because comparing the two integrations on a live cluster compares two views of
// the cluster as well as two integrations: the plugin sees its own reservations in the
// scheduler cache, while the extender refreshes bound pods periodically. Replaying the
// same snapshot through both paths removes that difference, leaving the decision cost.
// The extender's equivalent is POST /replay.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	extenderv1 "k8s.io/kube-scheduler/extender/v1"

	"github.com/leopoldch/kaptain/plugin/pkg/kaptain"
	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
	"github.com/leopoldch/kaptain/plugin/pkg/strategies"
)

func main() {
	strategyName := flag.String("strategy", strategies.DummyRandom, "strategy to replay")
	repeat := flag.Int("repeat", 1, "how many times to score the snapshot")
	path := flag.String("snapshot", "-", "snapshot JSON file, or - for stdin")
	flag.Parse()

	strategy, err := strategies.Get(*strategyName)
	if err != nil {
		fail(err)
	}

	current, err := readSnapshot(*path)
	if err != nil {
		fail(err)
	}
	current.Strategy = strategy.Name()

	for i := 0; i < *repeat; i++ {
		started := time.Now()
		scores, err := strategy.Scores(current)
		elapsed := time.Since(started)
		if err != nil {
			fail(err)
		}

		normalized, lowest, highest := map[string]int64{}, scores[current.Names()[0]], scores[current.Names()[0]]
		for _, value := range scores {
			if value < lowest {
				lowest = value
			}
			if value > highest {
				highest = value
			}
		}
		for name, value := range scores {
			normalized[name] = kaptain.QuantizeRaw(value, lowest, highest)
		}

		line, _ := json.Marshal(map[string]any{
			"integration":    kaptain.Integration,
			"mode":           "replay",
			"strategy":       strategy.Name(),
			"run_id":         current.RunID,
			"policy_version": current.PolicyVersion,
			"pod_task_id":    current.Pod.TaskID,
			"intended_node":  snapshot.Best(current.Names(), scores),
			"scores":         scores,
			"normalized":     normalized,
			"scale":          extenderv1.MaxExtenderPriority,
			"duration_ms":    float64(elapsed.Microseconds()) / 1000,
			"iteration":      i,
		})
		fmt.Println(string(line))
	}
}

// readSnapshot accepts either a bare snapshot or a fixture wrapping one, so the shared
// files in testdata/snapshots can be replayed directly.
func readSnapshot(path string) (*snapshot.Snapshot, error) {
	raw, err := os.ReadFile(path)
	if path == "-" {
		raw, err = readAll(os.Stdin)
	}
	if err != nil {
		return nil, err
	}

	var wrapper struct {
		Snapshot *snapshot.Snapshot `json:"snapshot"`
	}
	if err := json.Unmarshal(raw, &wrapper); err == nil && wrapper.Snapshot != nil {
		return wrapper.Snapshot, nil
	}

	var current snapshot.Snapshot
	if err := json.Unmarshal(raw, &current); err != nil {
		return nil, err
	}
	if len(current.Nodes) == 0 {
		return nil, fmt.Errorf("snapshot has no candidate nodes")
	}
	return &current, nil
}

func readAll(file *os.File) ([]byte, error) {
	buffer := make([]byte, 0, 4096)
	chunk := make([]byte, 4096)
	for {
		read, err := file.Read(chunk)
		buffer = append(buffer, chunk[:read]...)
		if err != nil {
			if read == 0 && len(buffer) == 0 {
				return nil, err
			}
			return buffer, nil
		}
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "kaptain-replay:", err)
	os.Exit(1)
}
