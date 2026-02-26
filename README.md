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
- **Based on Redis Queue Length**
  Periodically reads the Redis queue length (of type *List* using the `llen` query). The target *workers* are calculated proportionally to `tasks_per_worker`.

---

Superscaler consists of two components:
1. **Main Daemon (`superscaler`)**
2. **Supervisor RPC Plugin (`superscaler_plugin`)**

### Scaling Algorithm
For every configured target, the superscaler daemon periodically:
1. Retrieves the Redis queue depth (`llen`) and calculates: `desired_workers = ceil(queue_len / tasks_per_worker)`.
2. Bounds the `desired_workers` between `min_workers` and `max_workers`.
3. Checks the actual number of active workers currently running in Supervisor.
4. **If active < desired**: Emits a `scaleUp` RPC call (up to `scale_up_step`) if `cooldown_up` time has elapsed.
5. **If active > desired**: Emits a `scaleDown` RPC call (up to `scale_down_step`) if `cooldown_down` has elapsed AND there are no pending processes still in the middle of being stopped.

### How it Works (Under the Hood)
Because standard Supervisor does not support dynamic process additions/removals without disruptive reloading, this package provides a custom XML-RPC plugin (`[rpcinterface:superscaler]`).
* **Scaling Up:** The plugin dynamically increments `numprocs` within the `.ini` config on disk, re-parses it internally using Supervisor's built-in parser, and seamlessly instantiates new worker objects directly into the live supervisor memory dictionary. Specifically, the RPC plugin compares the newly parsed group configurations against the current in-memory process directory. For any new process name found (e.g. `worker_03`), it synthesizes a `Process` internal object using supervisor's `make_process()`, appends it to `group.processes` dict, and relies on supervisor's next main loop transition to naturally (`auto_spawn`) spark the process state to `STARTING`.
* **Scaling Down:** To avoid forcefully killing jobs, `scaleDown` merely sends graceful stop signals to higher-numbered processes first. The daemon periodically queries the state of these stopping workers via polling. Only when their states successfully transition to `STOPPED_STATES` (or after `pending_timeout`), the daemon fires `confirmScaleDown`. In this confirmation phase, the plugin rewrites the config file on disk to officially decrement `numprocs`, re-parses it, and finally deletes the stopped `Process` instances natively from the `group.processes` dictionary. This precise operation ordering prevents fatal divergence between the in-memory state and the configuration file if the system crashes midway.

---

## Usage

Requirements for superscaler are `python3.9` and `redis-py` minimum version `4.0.0`. This codebase provides standardized installation for `.rpm` and `.deb` distributions.

### Supervisor Configuration

Add the following plugin to your `supervisord.conf` configuration:
```ini
[rpcinterface:superscaler]
supervisor.rpcinterface_factory = superscaler_plugin.rpcinterface:SuperscalerNamespaceRPCInterface
```

### Superscaler Daemon Configuration

The default path for the superscaler configuration file is `/etc/superscaler/superscaler.conf`. All parameters below are mandatory and contain no default fallback mechanisms.

| Section / Parameter | Type | Description |
| :--- | :---: | :--- |
| **`[redis]`** | | **Redis connection configuration** |
| `host` | String | Redis server IP or hostname (e.g., `127.0.0.1`) |
| `port` | Integer | Redis port (e.g., `6379`) |
| `password` | String | Redis password. Leave blank if none. |
| `db` | Integer | Redis DB integer index (e.g., `0`) |
| **`[supervisor]`** | | **Supervisor integration settings** |
| `unix_socket_path`| String | The exact UNIX socket URI for XML-RPC (e.g., `unix:///var/run/supervisor.sock`) |
| `username` | String | Supervisor username. Leave blank if none. |
| `password` | String | Supervisor password. Leave blank if none. |

#### Target Configurations
Every target worker pool must be defined with `[target:<your_target_name>]` prefix. For instance, `[target:example-worker]`.

| Parameter | Type | Description |
| :--- | :---: | :--- |
| `queue_key` | String | The exact Redis list key to monitor using `llen`. |
| `group_name` | String | The exact Supervisor process group name. |
| `poll_interval` | Integer | Duration in seconds between queue checks. |
| `tasks_per_worker`| Integer | Expected pending tasks ratio assigned for each worker. |
| `min_workers` | Integer | Minimum boundary for worker process count. |
| `max_workers` | Integer | Maximum boundary for worker process count. |
| `scale_up_step` | Integer | The limit of workers to add per scaling up action. |
| `scale_down_step` | Integer | The limit of workers to remove per scaling down action. |
| `cooldown_up` | Integer | Safe duration in seconds to wait before allowing another scale up. |
| `cooldown_down` | Integer | Safe duration in seconds to wait before allowing another scale down. |
| `pending_timeout` | Integer | Duration in seconds to wait for a stopping worker to exit before the superscaler gives up on that stop confirmation logic. |