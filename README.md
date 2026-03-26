![Superscaler Logo](https://raw.githubusercontent.com/mijonOfTheMoon/images/refs/heads/main/latest-superscaler-logo.png)
# Superscaler

**Superscaler** is an *autoscaling* service for Supervisor *workers*. This service is designed to automatically add or remove the number of *worker* processes in Supervisor according to the incoming workload.

Autoscaling feature is not supported by Supervisor natively. However, Supervisor mentions in their documentation that they provide an RPC interface whose functionality can be extended.

```
Supervisor's XML-RPC interface may be extended arbitrarily by programmers. 
Additional top-level namespace XML-RPC interfaces can be added
using the [rpcinterface:foo] declaration in the configuration file.
```

---

- **Zero Downtime**

  Superscaler modifies the number of workers without needing to restart other workers that are currently processing the queue. Superscaler manipulates Supervisor's *in-memory process dictionary* through a custom plugin.

- **Pluggable Queue Backends**

  Supports multiple queue backends simultaneously. Each target can monitor a different queue system. Currently supported backends: **RabbitMQ** (via AMQP) and **Redis** (via list length). Adding new backends requires only subclassing `QueueMonitor` and registering in the backend registry.

---

Superscaler consists of two components:
1. **Main Daemon (`superscaler`)**
2. **Supervisor RPC Plugin (`superscaler_plugin`)**

### Scaling Algorithm
For every configured target, the superscaler daemon periodically based on `poll_interval` configuration.
1. Superscaler retrieves the queue depth from the configured backend and calculates: `desired_workers = ceil(queue_len / tasks_per_worker)`.
2. Bounds the `desired_workers` between `min_workers` and `max_workers`.
3. Checks the actual number of active workers currently running in Supervisor.
4. **If active < desired**: Emits a `scaleUp` RPC call (up to `scale_up_step`) if `cooldown_up` time has elapsed AND there are no pending processes still in the middle of being stopped.
5. **If active > desired**: Emits a `scaleDown` RPC call (up to `scale_down_step`) if `cooldown_down` has elapsed AND there are no pending processes still in the middle of being stopped.

### How it Works
Because standard Supervisor does not support dynamic process additions/removals without disruptive reloading, this package provides a custom XML-RPC plugin (`[rpcinterface:superscaler]`).

- **Scaling Up**

  The plugin dynamically increments `numprocs` within the `.ini` config on disk, re-parses it internally using Supervisor's built-in parser, and seamlessly instantiates new worker objects directly into the live supervisor memory dictionary. Specifically, the RPC plugin compares the newly parsed group configurations against the current in-memory process directory. For any new process name found (e.g. `worker_03`), it synthesizes a `Process` internal object using supervisor's `make_process()`, appends it to `group.processes` dict, and relies on supervisor's next main loop transition to naturally (`auto_spawn`) spark the process state to `STARTING`.

- **Scaling Down:**

  To avoid forcefully killing jobs, `scaleDown` just sends graceful stop signals to higher-numbered processes first. The daemon periodically queries the state of these stopping workers via polling. Only when their states successfully transition to `STOPPED_STATES`, the daemon fires `confirmScaleDown`. In this confirmation phase, the plugin rewrites the config file on disk to officially decrement `numprocs`, re-parses it, and finally deletes the stopped `Process` instances natively from the `group.processes` dictionary. This precise operation ordering prevents fatal divergence between the in-memory state and the configuration file if the system crashes midway.

---

## Clustering

Superscaler supports a master/slave clustering architecture to manage Supervisor workers across multiple servers.

- The **master node** runs the Superscaler daemon (`superscaler`). It reads the configuration, polls queues, and issues scaling commands to all configured nodes.
- **Slave nodes** only run Supervisor with the `superscaler_plugin` installed. They expose an HTTP XML-RPC endpoint so the master can reach them remotely. Slave nodes do **not** need the superscaler daemon running.

For local nodes (on the same machine as the daemon), Superscaler communicates via Unix socket. For remote nodes, it uses standard HTTP XML-RPC (`xmlrpc.client.ServerProxy`) with optional HTTP Basic Auth.

### `[node:<name>]` Section

Each node is defined with a `[node:<name>]` section in `superscaler.conf`. The name is a unique identifier you choose (e.g., `local`, `worker-1`).

| Parameter | Description |
| :--- | :--- |
| `url` | **Required.** Endpoint URL. Use `http://host:port/RPC2` for remote nodes or `unix:///path/to/socket` for local nodes. |
| `username` | Supervisor username for authentication. Leave blank if none. |
| `password` | Supervisor password for authentication. Leave blank if none. |

Example:

```ini
[node:local]
url = unix:///var/run/supervisor.sock
username =
password =

[node:worker-1]
url = http://192.168.1.10:9001/RPC2
username = admin
password = secret

[node:worker-2]
url = http://192.168.1.11:9001/RPC2
username = admin
password = secret
```

### `nodes` Parameter on Targets

Each `[target:*]` section accepts a `nodes` parameter — a comma-separated list of `[node:<name>]` names that the target should scale across.

```ini
[target:main-scaler]
type = supervisor
queue = main-rabbit
queue_key = tasks
program_name = example-worker
nodes = local, worker-1, worker-2
tasks_per_worker = 50
min_workers = 2
max_workers = 30
```

When scaling up, workers are added to the node with the fewest active workers first (least-loaded). When scaling down, workers are removed from the node with the most active workers first (most-loaded).

> **Backward compatibility:** If no `[node:*]` sections are defined but a `[supervisor]` section exists, Superscaler automatically creates a default node from the `[supervisor]` settings. Existing configurations continue to work without changes.

---

## Installation Guide

Requirements for superscaler are `python3.9`, `redis-py` minimum version `4.0.0`, and `pika` minimum version `1.2.0`. This codebase provides standardized installation for `.rpm` and `.deb` distributions.

### Red Hat / CentOS

1. Download the RPM package

```bash
curl -LO https://github.com/mijonOfTheMoon/superscaler/releases/download/3.0.0/superscaler-3.0.0-1.amzn2023.noarch.rpm
```

2. Install the package

```bash
sudo dnf install superscaler-3.0.0-1.amzn2023.noarch.rpm
```

### Debian / Ubuntu

1. Download the DEB package

```bash
curl -LO https://github.com/mijonOfTheMoon/superscaler/releases/download/3.0.0/superscaler_3.0.0-1_all.deb
```

2. Install the package

```bash
sudo dpkg -i superscaler_3.0.0-1_all.deb
```

> **Note:** The superscaler service is **not** enabled or started automatically after installation. This is intentional — slave nodes only need the plugin, not the daemon. See the sections below for setup instructions.

### Master Node Setup

The master node runs the Superscaler daemon and coordinates scaling across all nodes.

1. Install the package (RPM or DEB as shown above).

2. Configure `/etc/superscaler/superscaler.conf` with `[node:<name>]` sections for each Supervisor node in your cluster:

```ini
[node:local]
url = unix:///var/run/supervisor.sock
username =
password =

[node:worker-1]
url = http://192.168.1.10:9001/RPC2
username = admin
password = secret
```

3. Define your targets with the `nodes` parameter referencing the configured nodes:

```ini
[target:main-scaler]
type = supervisor
queue = main-rabbit
queue_key = tasks
program_name = example-worker
nodes = local, worker-1
tasks_per_worker = 50
min_workers = 2
max_workers = 20
```

4. Enable and start the superscaler service:

```bash
sudo systemctl enable superscaler
sudo systemctl start superscaler
```

5. If the master node also runs Supervisor workers, add the plugin to the local `supervisord.conf`:

```ini
[rpcinterface:superscaler]
supervisor.rpcinterface_factory = superscaler_plugin.rpcinterface:SuperscalerNamespaceRPCInterface
```

Then restart Supervisor:

```bash
sudo systemctl restart supervisor
```

### Slave Node Setup

Slave nodes run Supervisor with the plugin and expose an HTTP XML-RPC endpoint. The superscaler daemon does **not** need to run on slave nodes.

1. Install the package (RPM or DEB as shown above). The service stays disabled by default — no extra steps needed to keep it off.

2. Add the `[rpcinterface:superscaler]` plugin to `/etc/supervisor/supervisord.conf`:

```ini
[rpcinterface:superscaler]
supervisor.rpcinterface_factory = superscaler_plugin.rpcinterface:SuperscalerNamespaceRPCInterface
```

3. Enable the `[inet_http_server]` section in `/etc/supervisor/supervisord.conf` so the master can reach this node over HTTP:

```ini
[inet_http_server]
port = 0.0.0.0:9001
username = admin
password = secret
```

Use a strong password and restrict network access (firewall rules, private network) to protect the endpoint.

4. Restart Supervisor to apply the changes:

```bash
sudo systemctl restart supervisor
```

That's it for the slave. No superscaler service needs to be enabled or started — the master node handles all scaling decisions remotely.

## Usage

Add the following plugin to your `supervisord.conf` configuration:
```ini
[rpcinterface:superscaler]
supervisor.rpcinterface_factory = superscaler_plugin.rpcinterface:SuperscalerNamespaceRPCInterface
```

After adding the plugin, configure the superscaler. The default path for the superscaler configuration file is `/etc/superscaler/superscaler.conf`.

#### `[supervisor]` Section
Configures the communication layer to the Supervisor daemon.

| Parameter | Description |
| :--- | :--- |
| `unix_socket_path`| The exact UNIX socket URI for XML-RPC (e.g., `unix:///var/run/supervisor.sock`) |
| `username` | Supervisor username. Leave blank if none. |
| `password` | Supervisor password. Leave blank if none. |

#### `[queue:<name>]` Section
Defines a named queue backend. Multiple backends can be configured simultaneously. The `type` parameter selects the backend driver.

| Parameter | Description |
| :--- | :--- |
| `type` | **Required.** Backend type: `rabbitmq` or `redis` |

**RabbitMQ parameters** (`type = rabbitmq`):

| Parameter | Description |
| :--- | :--- |
| `host` | RabbitMQ server hostname (e.g., `127.0.0.1`) |
| `port` | AMQP port (e.g., `5672`) |
| `username` | RabbitMQ username (e.g., `guest`) |
| `password` | RabbitMQ password (e.g., `guest`) |
| `vhost` | Virtual host (e.g., `/`) |

**Redis parameters** (`type = redis`):

| Parameter | Description |
| :--- | :--- |
| `host` | Redis server IP or hostname (e.g., `127.0.0.1`) |
| `port` | Redis port (e.g., `6379`) |
| `password` | Redis password. Leave blank if none. |
| `db` | Redis DB integer index (e.g., `0`) |

#### `[target:<your_target_name>]` Section
Every target worker pool must be defined with `[target:<your_target_name>]` prefix. For instance, `[target:example-scaler]`.

| Parameter | Description |
| :--- | :--- |
| `queue` | **Required.** Name of a `[queue:*]` section to use as the queue backend. |
| `queue_key` | **Required.** The queue key or name to monitor in the backend. |
| `program_name` | **Required.** The exact Supervisor program name to be autoscaled. |
| `tasks_per_worker`| **Required.** Expected pending tasks ratio assigned for each worker. |
| `min_workers` | **Required.** Minimum boundary for worker process count. |
| `max_workers` | **Required.** Maximum boundary for worker process count. |
| `poll_interval` | *Optional.* Duration in seconds between queue checks. Defaults to `10`. |
| `scale_up_step` | *Optional.* The limit of workers to add per scaling up action. Defaults to `1`. |
| `scale_down_step` | *Optional.* The limit of workers to remove per scaling down action. Defaults to `1`. |
| `cooldown_up` | *Optional.* Safe duration in seconds to wait before allowing another scale up. Defaults to `0`. |
| `cooldown_down` | *Optional.* Safe duration in seconds to wait before allowing another scale down. Defaults to `0`. |
| `nodes` | *Optional.* Comma-separated list of `[node:<name>]` names for multi-node scaling. See [Clustering](#clustering). |

### Post Configuration

For a single-node setup using the `[supervisor]` section, restart both services:

```bash
sudo systemctl restart supervisor
sudo systemctl restart superscaler
```

For a multi-node cluster setup, see [Master Node Setup](#master-node-setup) and [Slave Node Setup](#slave-node-setup) above.