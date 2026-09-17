package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"k8s.io/apimachinery/pkg/util/yaml"
)

func main() {
	files, _ := filepath.Glob("k8s/*.yaml")
	files = append(files, "kind-cluster.yaml")
	failed := false
	for _, f := range files {
		if strings.Contains(f, " 2.yaml") {
			continue
		}
		raw, err := os.ReadFile(f)
		if err != nil {
			fmt.Println("FAIL", f, err)
			failed = true
			continue
		}
		kinds := []string{}
		for _, doc := range strings.Split(string(raw), "\n---") {
			if strings.TrimSpace(doc) == "" {
				continue
			}
			var parsed struct {
				Kind string `json:"kind"`
			}
			if err := yaml.Unmarshal([]byte(doc), &parsed); err != nil {
				fmt.Println("FAIL", f, err)
				failed = true
				continue
			}
			kinds = append(kinds, parsed.Kind)
		}
		fmt.Printf("ok   %-40s %v\n", f, kinds)
	}
	if failed {
		os.Exit(1)
	}
}
