// Package decider calls an out-of-process decider (ML, RL or LLM) over HTTP. It is the
// plugin's option 2: the integration stays inside the scheduler, the policy runs outside.
// No model is trained or served in the scheduler process.
package decider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/leopoldch/kaptain/plugin/pkg/snapshot"
)

type Response struct {
	Node   string             `json:"node"`
	Scores map[string]float64 `json:"scores,omitempty"`
}

type Client struct {
	URL     string
	Timeout time.Duration
	http    *http.Client
}

func New(url string, timeout time.Duration) *Client {
	return &Client{URL: url, Timeout: timeout, http: &http.Client{Timeout: timeout}}
}

// Decide returns raw scores per node. A decider may answer with either a single node or a
// full score map; a single node becomes a one-hot score so both shapes reach Score the
// same way. Every failure is an error: the caller then applies the explicit fallback.
func (c *Client) Decide(ctx context.Context, s *snapshot.Snapshot) (map[string]float64, error) {
	body, err := json.Marshal(s)
	if err != nil {
		return nil, fmt.Errorf("marshal snapshot: %w", err)
	}

	ctx, cancel := context.WithTimeout(ctx, c.Timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.URL, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("call decider: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("decider returned %s", resp.Status)
	}

	var decoded Response
	if err := json.NewDecoder(resp.Body).Decode(&decoded); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}
	return scores(s, decoded)
}

func scores(s *snapshot.Snapshot, r Response) (map[string]float64, error) {
	if len(r.Scores) > 0 {
		for name := range r.Scores {
			if !s.Has(name) {
				return nil, fmt.Errorf("decider scored unknown node %q", name)
			}
		}
		return r.Scores, nil
	}
	if r.Node == "" {
		return nil, fmt.Errorf("decider returned neither node nor scores")
	}
	if !s.Has(r.Node) {
		return nil, fmt.Errorf("decider chose unknown node %q", r.Node)
	}
	out := make(map[string]float64, len(s.Nodes))
	for _, n := range s.Nodes {
		out[n.Name] = 0
	}
	out[r.Node] = 1
	return out, nil
}
