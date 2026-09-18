package snapshot

import "testing"

func TestBestBreaksTiesByName(t *testing.T) {
	names := []string{"c", "a", "b"}
	scores := map[string]float64{"a": 1, "b": 1, "c": 1}
	if got := Best(names, scores); got != "a" {
		t.Fatalf("got %q, want a", got)
	}
}

func TestBestIgnoresUnscoredNodes(t *testing.T) {
	if got := Best([]string{"a", "b"}, map[string]float64{"b": 0.5}); got != "b" {
		t.Fatalf("got %q, want b", got)
	}
}

func TestBestReturnsEmptyWithoutScores(t *testing.T) {
	if got := Best([]string{"a"}, nil); got != "" {
		t.Fatalf("got %q, want empty", got)
	}
}

func TestPickIsDeterministicAndOrderIndependent(t *testing.T) {
	first := Pick([]string{"a", "b", "c"}, 7, "pod-1")
	second := Pick([]string{"c", "b", "a"}, 7, "pod-1")
	if first != second {
		t.Fatalf("pick depends on input order: %q vs %q", first, second)
	}
	if first == "" {
		t.Fatal("pick returned nothing")
	}
}

func TestPickHandlesEmptyInput(t *testing.T) {
	if got := Pick(nil, 1, "pod"); got != "" {
		t.Fatalf("got %q, want empty", got)
	}
}

func TestTaskIDPrefersAStableIdentity(t *testing.T) {
	labels := map[string]string{TaskIDLabel: "task-7"}
	if got := TaskID("uid-1", "default", "pod-a", labels, nil); got != "task-7" {
		t.Fatalf("got %q, want the label value", got)
	}
	annotations := map[string]string{TaskIDAnnotation: "task-8"}
	if got := TaskID("uid-1", "default", "pod-a", nil, annotations); got != "task-8" {
		t.Fatalf("got %q, want the annotation value", got)
	}
	if got := TaskID("uid-1", "default", "pod-a", nil, nil); got != "default/pod-a" {
		t.Fatalf("got %q, want namespace/name", got)
	}
	if got := TaskID("uid-1", "", "", nil, nil); got != "uid-1" {
		t.Fatalf("got %q, want the uid as last resort", got)
	}
}

func TestSeededDrawSurvivesPodRecreation(t *testing.T) {
	names := []string{"a", "b", "c"}
	// Same workload, recreated: new UID, same name. The draw must not move.
	first := Pick(names, 7, TaskID("uid-1", "default", "job-1", nil, nil))
	second := Pick(names, 7, TaskID("uid-2", "default", "job-1", nil, nil))
	if first != second {
		t.Fatalf("draw moved on recreation: %q then %q", first, second)
	}
}
