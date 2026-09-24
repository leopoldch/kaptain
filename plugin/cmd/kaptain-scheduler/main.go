package main

import (
	"os"

	"k8s.io/component-base/cli"
	"k8s.io/kubernetes/cmd/kube-scheduler/app"

	"github.com/leopoldch/kaptain/plugin/pkg/kaptain"
)

func main() {
	command := app.NewSchedulerCommand(
		app.WithPlugin(kaptain.Name, kaptain.New),
	)
	os.Exit(cli.Run(command))
}
