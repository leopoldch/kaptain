package decider

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
)

func testSnapshot() *snapshot.Snapshot {
	return &snapshot.Snapshot{
		Pod:   snapshot.Pod{UID: "pod-1"},
		Nodes: []snapshot.Node{{Name: "a"}, {Name: "b"}},
	}
}

func serve(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return New(server.URL, 200*time.Millisecond)
}

func TestSingleNodeAnswerBecomesOneHotScores(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{"node":"b"}`))
	})
	scores, err := client.Decide(context.Background(), testSnapshot())
	if err != nil {
		t.Fatal(err)
	}
	if scores["b"] != 1 || scores["a"] != 0 {
		t.Fatalf("unexpected scores: %v", scores)
	}
}

func TestScoreMapAnswerIsKept(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{"scores":{"a":0.2,"b":0.8}}`))
	})
	scores, err := client.Decide(context.Background(), testSnapshot())
	if err != nil {
		t.Fatal(err)
	}
	if scores["a"] != 0.2 || scores["b"] != 0.8 {
		t.Fatalf("unexpected scores: %v", scores)
	}
}

func TestUnknownNodeIsRejected(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{"node":"ghost"}`))
	})
	if _, err := client.Decide(context.Background(), testSnapshot()); err == nil {
		t.Fatal("an unknown node was accepted")
	}
}

func TestEmptyAnswerIsRejected(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{}`))
	})
	if _, err := client.Decide(context.Background(), testSnapshot()); err == nil {
		t.Fatal("an empty answer was accepted")
	}
}

func TestMalformedAnswerIsRejected(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`not json`))
	})
	if _, err := client.Decide(context.Background(), testSnapshot()); err == nil {
		t.Fatal("malformed JSON was accepted")
	}
}

func TestErrorStatusIsRejected(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	if _, err := client.Decide(context.Background(), testSnapshot()); err == nil {
		t.Fatal("a 500 answer was accepted")
	}
}

func TestTimeoutIsAnError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(150 * time.Millisecond)
		w.Write([]byte(`{"node":"a"}`))
	}))
	t.Cleanup(server.Close)

	client := New(server.URL, 20*time.Millisecond)
	started := time.Now()
	if _, err := client.Decide(context.Background(), testSnapshot()); err == nil {
		t.Fatal("a slow decider was accepted")
	}
	if elapsed := time.Since(started); elapsed > 100*time.Millisecond {
		t.Fatalf("timeout not enforced: waited %s", elapsed)
	}
}

func TestUnreachableDeciderIsAnError(t *testing.T) {
	client := New("http://127.0.0.1:1/choose", 20*time.Millisecond)
	if _, err := client.Decide(context.Background(), testSnapshot()); err == nil {
		t.Fatal("an unreachable decider was accepted")
	}
}
