require "etc"
require "fileutils"
require "securerandom"
require "yaml"

Vagrant.require_version ">= 2.4.9"  # the first that knows VirtualBox 7.2

HERE = File.dirname(__FILE__)
SETTINGS = YAML.load_file(File.join(HERE, "vagrant.yaml"))
MASTER = SETTINGS["master"]
WORKERS = SETTINGS["nodes"]
MASTER_IP = "192.168.56.10"
K3S_VERSION = "v1.31.0+k3s1"  # the Kubernetes version the plugin is compiled against

token_file = File.join(HERE, ".vagrant", "k3s-token")
unless File.exist?(token_file)
  FileUtils.mkdir_p(File.dirname(token_file))
  File.write(token_file, SecureRandom.hex(32))
end
TOKEN = File.read(token_file).strip

vcpus = ([MASTER] + WORKERS).sum { |node| node["cpus"] }
if vcpus > Etc.nprocessors
  warn "vagrant.yaml asks for #{vcpus} vCPUs, this machine has #{Etc.nprocessors}: measurements will suffer."
end

def size(machine, node)
  machine.vm.provider "virtualbox" do |vb|
    vb.name = "kaptain-#{node["name"]}"
    vb.cpus = node["cpus"]
    vb.memory = node["memory"]
    vb.customize ["modifyvm", :id, "--cpuexecutioncap", (node["cpu_cap"] || 100).to_s]
  end
end

Vagrant.configure("2") do |config|
  config.vm.box = "bento/ubuntu-24.04"
  config.vm.box_version = "202510.26.0"
  config.vm.boot_timeout = 600
  config.vbguest.auto_update = false if Vagrant.has_plugin?("vagrant-vbguest")

  # Workers first: the master, last, waits for them before it deploys and runs anything.
  WORKERS.each_with_index do |node, index|
    config.vm.define node["name"] do |machine|
      ip = "192.168.56.#{11 + index}"
      machine.vm.hostname = node["name"]
      machine.vm.network "private_network", ip: ip, virtualbox__intnet: "kaptain"
      # The pods run here: no .env on these machines.
      machine.vm.synced_folder ".", "/vagrant", disabled: true
      size(machine, node)
      machine.vm.provision "setup", type: "shell", path: "vagrant/worker.sh", env: {
        "NODE_NAME" => node["name"], "NODE_IP" => ip, "MASTER_IP" => MASTER_IP,
        "K3S_VERSION" => K3S_VERSION, "K3S_TOKEN" => TOKEN,
      }
    end
  end

  config.vm.define MASTER["name"], primary: true do |machine|
    machine.vm.hostname = MASTER["name"]
    machine.vm.network "private_network", ip: MASTER_IP, virtualbox__intnet: "kaptain"
    machine.vm.synced_folder ".", "/vagrant", type: "virtualbox"
    size(machine, MASTER)
    machine.vm.provision "setup", type: "shell", path: "vagrant/master.sh", env: {
      "NODE_NAME" => MASTER["name"], "NODE_IP" => MASTER_IP, "K3S_VERSION" => K3S_VERSION, "K3S_TOKEN" => TOKEN,
      "NODE_COUNT" => (WORKERS.size + 1).to_s,
      "EXPERIMENT_NODES" => ([MASTER] + WORKERS).select { |node| node["experiment"] }.map { |node| node["name"] }.join(" "),
    }
    campaign = SETTINGS["campaign"]
    machine.vm.provision "experiments", type: "shell", path: "vagrant/experiments.sh", env: {
      "MASTER_NAME" => MASTER["name"], "EXPERIMENTS" => campaign["experiments"], "STRATEGIES" => campaign["strategies"],
      "SEEDS" => campaign["seeds"].to_s, "TRAIN_RUNS" => campaign["train_runs"].to_s,
      "REPLICATES" => campaign["replicates"].to_s, "VALIDATE_EVERY" => campaign["validate_every"].to_s,
      "TRAIN_ON" => campaign["train_on"].to_s,
    }
  end
end
