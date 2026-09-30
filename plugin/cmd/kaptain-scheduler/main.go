package main

import (
	"os"

	"k8s.io/component-base/cli"
	"k8s.io/kubernetes/cmd/kube-scheduler/app"

	"kaptain/scoring"
)

func main() {
	command := app.NewSchedulerCommand(
		app.WithPlugin(scoring.Name, scoring.New),
	)
	os.Exit(cli.Run(command))
}
