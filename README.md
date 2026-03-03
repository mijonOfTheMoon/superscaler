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
* **Scaling Up:** The plugin dynamically increments `numprocs` within the `.ini` config on disk, re-parses it internally using Supervisor's built-in parser, and seamlessly instantiates new worker objects directly into the live supervisor memory dictionary. Specifically, the RPC plugin compares the newly parsed group configurations against the current in-memory process directory. For any new process name found (e.g. `worker_03`), it synthesizes a `Process` internal object using supervisor's `make_process()`, appends it to `group.processes` dict, and relies on supervisor's next main loop transition to naturally (`auto_spawn`) spark the process state to `STARTING`.
* **Scaling Down:** To avoid forcefully killing jobs, `scaleDown` just sends graceful stop signals to higher-numbered processes first. The daemon periodically queries the state of these stopping workers via polling. Only when their states successfully transition to `STOPPED_STATES`, the daemon fires `confirmScaleDown`. In this confirmation phase, the plugin rewrites the config file on disk to officially decrement `numprocs`, re-parses it, and finally deletes the stopped `Process` instances natively from the `group.processes` dictionary. This precise operation ordering prevents fatal divergence between the in-memory state and the configuration file if the system crashes midway.

---

## Installation Guide

Requirements for superscaler are `python3.9`, `redis-py` minimum version `4.0.0`, and `pika` minimum version `1.2.0`. This codebase provides standardized installation for `.rpm` and `.deb` distributions.

### Red Hat / CentOS

1. Download the RPM package

```bash
curl -LO https://github.com/mijonOfTheMoon/superscaler/releases/download/2.1.0/superscaler-2.1.0-1.amzn2023.noarch.rpm
```

2. Install the package

```bash
sudo dnf install superscaler-2.1.0-1.amzn2023.noarch.rpm
```

### Debian / Ubuntu

1. Download the DEB package

```bash
curl -LO https://github.com/mijonOfTheMoon/superscaler/releases/download/2.1.0/superscaler_2.1.0-1_all.deb
```

2. Install the package

```bash
sudo dpkg -i superscaler_2.1.0-1_all.deb
```

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

### Post Configuration

After configuring the superscaler, you need to restart the supervisor and superscaler services to apply the changes.

```bash
sudo systemctl restart supervisor
sudo systemctl restart superscaler
```