package kaptain

import (
	"os"
	"path/filepath"
	"testing"

	"k8s.io/apimachinery/pkg/util/yaml"
	schedconfig "k8s.io/kubernetes/pkg/scheduler/apis/config"
	"k8s.io/kubernetes/pkg/scheduler/apis/config/scheme"
	"k8s.io/kubernetes/pkg/scheduler/framework/runtime"
)

const manifest = "../../../k8s/scheduler-plugin-config.yaml"

// TestProfileLoadsThePlugin is the minimal local validation that kube-scheduler would
// accept the shipped profile: the real KubeSchedulerConfiguration decoder parses it, the
// plugin is enabled at the extension points it implements, and "disabled: *" does not
// accidentally switch it off.
func TestProfileLoadsThePlugin(t *testing.T) {
	config := decodeProfile(t)

	if len(config.Profiles) != 1 {
		t.Fatalf("got %d profiles, want 1", len(config.Profiles))
	}
	profile := config.Profiles[0]
	if profile.SchedulerName != "kaptain-scheduler" {
		t.Fatalf("scheduler name %q", profile.SchedulerName)
	}

	for _, extension := range []struct {
		name string
		set  schedconfig.PluginSet
	}{
		{"score", profile.Plugins.Score},
		{"preScore", profile.Plugins.PreScore},
		{"reserve", profile.Plugins.Reserve},
		{"postBind", profile.Plugins.PostBind},
	} {
		if !enabled(extension.set, Name) {
			t.Errorf("%s: plugin %q is not enabled", extension.name, Name)
		}
		if disabled(extension.set, Name) {
			t.Errorf("%s: plugin %q is explicitly disabled", extension.name, Name)
		}
	}

	// Filter must stay Kubernetes' job: the plugin scores, it does not constrain.
	if enabled(profile.Plugins.Filter, Name) {
		t.Error("the plugin is enabled at Filter: a decision would become a constraint")
	}

	if !disabled(profile.Plugins.Score, "*") {
		t.Error("native score plugins are not disabled: our score would only nudge the default one")
	}
}

// TestPluginIsRegistrable checks the factory signature the scheduler will call.
func TestPluginIsRegistrable(t *testing.T) {
	registry := runtime.Registry{}
	if err := registry.Register(Name, New); err != nil {
		t.Fatalf("register %q: %v", Name, err)
	}
	if _, ok := registry[Name]; !ok {
		t.Fatalf("plugin %q missing from the registry", Name)
	}
}

func TestStrategyFromEnvIsValidated(t *testing.T) {
	t.Setenv("KAPTAIN_STRATEGY", "does-not-exist")
	if _, err := New(nil, nil, nil); err == nil {
		t.Fatal("an unknown strategy was accepted at startup")
	}

	t.Setenv("KAPTAIN_STRATEGY", "least-allocated")
	plugin, err := New(nil, nil, nil)
	if err != nil {
		t.Fatalf("valid strategy rejected: %v", err)
	}
	if plugin.Name() != Name {
		t.Fatalf("plugin name %q, want %q", plugin.Name(), Name)
	}
}

func decodeProfile(t *testing.T) *schedconfig.KubeSchedulerConfiguration {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(manifest))
	if err != nil {
		t.Fatalf("read %s: %v", manifest, err)
	}

	var configMap struct {
		Data struct {
			Config string `json:"config.yaml"`
		} `json:"data"`
	}
	if err := yaml.Unmarshal(raw, &configMap); err != nil {
		t.Fatalf("parse configmap: %v", err)
	}
	if configMap.Data.Config == "" {
		t.Fatal("configmap has no config.yaml entry")
	}

	object, _, err := scheme.Codecs.UniversalDecoder().Decode([]byte(configMap.Data.Config), nil, nil)
	if err != nil {
		t.Fatalf("kube-scheduler would reject this config: %v", err)
	}
	config, ok := object.(*schedconfig.KubeSchedulerConfiguration)
	if !ok {
		t.Fatalf("decoded %T, want KubeSchedulerConfiguration", object)
	}
	return config
}

func enabled(set schedconfig.PluginSet, name string) bool {
	for _, plugin := range set.Enabled {
		if plugin.Name == name {
			return true
		}
	}
	return false
}

func disabled(set schedconfig.PluginSet, name string) bool {
	for _, plugin := range set.Disabled {
		if plugin.Name == name {
			return true
		}
	}
	return false
}
