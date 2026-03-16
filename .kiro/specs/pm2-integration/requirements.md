# Requirements Document

## Introduction

Superscaler saat ini hanya mendukung Supervisor sebagai process manager. Fitur ini menambahkan dukungan PM2 dengan pendekatan minimal: tambahkan field `type` di setiap section `[target:*]` pada konfigurasi. Jika `type = pm2`, ScalerEngine akan menggunakan perintah CLI PM2 (`pm2 scale`, `pm2 jlist`, `pm2 ping`) untuk mengelola proses, bukan Supervisor RPC. Jika `type = supervisor` (atau tidak diisi), semua berjalan seperti sekarang. Tidak ada abstract class, tidak ada refactor engine, tidak ada section `[manager]` baru.

## Glossary

- **ScalerEngine**: Loop scaling utama di `scaler.py` yang mengevaluasi target dan menjalankan operasi scale up/down.
- **SupervisorClient**: Client XML-RPC yang sudah ada untuk berkomunikasi dengan Supervisor via unix socket.
- **PM2**: Process manager Node.js dengan fitur scaling bawaan via `pm2 scale` dan listing proses via `pm2 jlist`.
- **Target**: Satu section `[target:*]` di konfigurasi yang memetakan sebuah queue ke sebuah process group.
- **Target_Type**: Field `type` pada section `[target:*]` yang menentukan apakah target dikelola oleh Supervisor atau PM2.
- **Config_Parser**: Modul `config.py` yang mem-parsing file konfigurasi superscaler.

## Requirements

### Requirement 1: Target Type Configuration Field

**User Story:** Sebagai operator, saya ingin menentukan tipe process manager per target di konfigurasi, agar saya bisa mencampur target Supervisor dan PM2 dalam satu instance superscaler.

#### Acceptance Criteria

1. THE Config_Parser SHALL support an optional `type` parameter in each `[target:*]` section with allowed values `supervisor` and `pm2`.
2. WHEN the `type` parameter is omitted from a `[target:*]` section, THE Config_Parser SHALL default the value to `supervisor`.
3. IF the `type` value is not `supervisor` or `pm2`, THEN THE Config_Parser SHALL raise a configuration error listing the supported types.
4. THE TargetConfig dataclass SHALL include a `type` field of type string.

### Requirement 2: Supervisor Target Behavior Unchanged

**User Story:** Sebagai operator yang sudah menggunakan Supervisor, saya ingin target bertipe `supervisor` tetap bekerja persis seperti sekarang, tanpa perubahan perilaku apapun.

#### Acceptance Criteria

1. WHEN a target has `type = supervisor`, THE ScalerEngine SHALL use the existing SupervisorClient for `get_group_info`, `scale_up`, `scale_down`, and `confirm_scale_down` operations.
2. WHEN a target has `type = supervisor`, THE ScalerEngine SHALL track pending scale-down processes and call `confirm_scale_down` after processes have stopped.
3. THE `[supervisor]` configuration section SHALL remain unchanged and continue to be required when at least one target has `type = supervisor`.

### Requirement 3: PM2 Scale Up

**User Story:** Sebagai operator, saya ingin superscaler bisa menambah worker PM2 secara otomatis saat queue membengkak, agar task diproses lebih cepat.

#### Acceptance Criteria

1. WHEN a PM2 target needs to scale up, THE ScalerEngine SHALL execute `pm2 scale <program_name> +<count>` as a subprocess.
2. IF the `pm2 scale` command exits with a non-zero code, THEN THE ScalerEngine SHALL log the error and skip the scale-up action for that tick.

### Requirement 4: PM2 Scale Down

**User Story:** Sebagai operator, saya ingin superscaler bisa mengurangi worker PM2 secara otomatis saat queue sepi, agar resource tidak terbuang.

#### Acceptance Criteria

1. WHEN a PM2 target needs to scale down, THE ScalerEngine SHALL execute `pm2 scale <program_name> <desired_count>` as a subprocess, where `desired_count` is the target number of active processes after removal.
2. THE ScalerEngine SHALL NOT track pending scale-down state for PM2 targets because PM2 removes processes immediately.
3. IF the `pm2 scale` command exits with a non-zero code, THEN THE ScalerEngine SHALL log the error and skip the scale-down action for that tick.

### Requirement 5: PM2 Get Group Info

**User Story:** Sebagai developer, saya ingin superscaler bisa membaca jumlah dan status proses PM2, agar engine bisa menghitung kebutuhan scaling.

#### Acceptance Criteria

1. WHEN a PM2 target tick is processed, THE ScalerEngine SHALL execute `pm2 jlist` and parse the JSON output.
2. THE ScalerEngine SHALL filter the `pm2 jlist` output by matching the `name` field to the target `program_name`.
3. THE ScalerEngine SHALL map PM2 statuses to ScalerEngine statenames: `online` to `RUNNING`, `stopping` to `STOPPING`, `stopped` to `STOPPED`, `errored` to `FATAL`, and `launching` to `STARTING`.
4. THE ScalerEngine SHALL return a dict with `count` and `processes` keys, matching the format returned by the existing SupervisorClient `get_group_info` method.
5. IF the `pm2 jlist` command fails, THEN THE ScalerEngine SHALL log a warning and skip the tick for that target.

### Requirement 6: PM2 Ping

**User Story:** Sebagai operator, saya ingin superscaler mengecek apakah PM2 tersedia saat startup, agar kesalahan konfigurasi terdeteksi lebih awal.

#### Acceptance Criteria

1. WHEN the daemon starts and at least one target has `type = pm2`, THE Main_Module SHALL execute `pm2 ping` to verify PM2 is reachable.
2. IF `pm2 ping` fails, THEN THE Main_Module SHALL log an error and exit with a non-zero code.

### Requirement 7: ScalerEngine Target Type Dispatch

**User Story:** Sebagai developer, saya ingin ScalerEngine memilih metode yang tepat berdasarkan `type` target, agar satu engine bisa menangani campuran target Supervisor dan PM2.

#### Acceptance Criteria

1. WHEN processing a target tick, THE ScalerEngine SHALL check the target `type` field to determine which process management methods to call.
2. WHEN the target `type` is `supervisor`, THE ScalerEngine SHALL call SupervisorClient methods and use the existing pending state tracking logic.
3. WHEN the target `type` is `pm2`, THE ScalerEngine SHALL call PM2 CLI helper functions and skip pending state tracking.
4. THE ScalerEngine SHALL retain all existing scaling logic for both types: cooldown timers, step limits, and min/max worker bounds.

### Requirement 8: PM2 CLI Helper Functions

**User Story:** Sebagai developer, saya ingin fungsi-fungsi helper untuk memanggil PM2 CLI, agar kode scaling tetap bersih dan mudah di-test.

#### Acceptance Criteria

1. THE PM2 helper module SHALL provide functions: `pm2_get_group_info(program_name)`, `pm2_scale_up(program_name, count)`, `pm2_scale_down(program_name, desired_count)`, and `pm2_ping()`.
2. THE PM2 helper functions SHALL execute PM2 commands via `subprocess.run` with a timeout.
3. IF any PM2 subprocess call exceeds the timeout, THEN THE PM2 helper function SHALL raise an exception.
4. THE `pm2_get_group_info` function SHALL parse the JSON output from `pm2 jlist` and return a dict in the same format as `SupervisorClient.get_group_info`.
5. THE `pm2_scale_up` function SHALL return a list of process names that were added, derived from comparing process lists before and after the scale command.
6. THE `pm2_ping` function SHALL return True when `pm2 ping` exits with code 0, and False otherwise.

### Requirement 9: PM2 Execution Environment Configuration

**User Story:** Sebagai operator yang menjalankan superscaler sebagai systemd service, saya ingin bisa mengkonfigurasi path PM2 binary, PM2_HOME, dan user eksekusi per target, agar tidak terjadi error permission atau "pm2 not found" saat service berjalan sebagai user berbeda dari pemilik instalasi PM2.

#### Acceptance Criteria

1. THE Config_Parser SHALL support an optional `pm2_path` parameter in each `[target:*]` section, specifying the full path to the PM2 binary, defaulting to `pm2` (resolved from PATH) when omitted.
2. THE Config_Parser SHALL support an optional `pm2_home` parameter in each `[target:*]` section, specifying the PM2_HOME directory path (e.g. `/home/deploy/.pm2`).
3. THE Config_Parser SHALL support an optional `run_as_user` parameter in each `[target:*]` section, specifying the OS user under which PM2 commands are executed via `sudo -u`.
4. THE TargetConfig dataclass SHALL include `pm2_path`, `pm2_home`, and `run_as_user` fields of type string, each defaulting to an empty string.
5. WHEN `pm2_home` is configured for a target, THE PM2 helper functions SHALL set the `PM2_HOME` environment variable to the configured value before executing PM2 subprocess commands.
6. WHEN `run_as_user` is configured for a target, THE PM2 helper functions SHALL prefix PM2 commands with `sudo -u <run_as_user>`.
7. WHEN both `run_as_user` and `pm2_home` are configured, THE PM2 helper functions SHALL pass `PM2_HOME` as an environment variable within the `sudo -u` command execution context.
8. WHEN `pm2_path` is configured for a target, THE PM2 helper functions SHALL use the configured path instead of the default `pm2` command.
9. WHEN the daemon starts and a PM2 target has `pm2_path` configured, THE Main_Module SHALL verify that the file at `pm2_path` exists and is executable.
10. IF the configured `pm2_path` does not exist or is not executable, THEN THE Main_Module SHALL log an error specifying the path and target name, and exit with a non-zero code.
11. IF `pm2_path`, `pm2_home`, or `run_as_user` are set on a target with `type = supervisor`, THEN THE Config_Parser SHALL log a warning that these parameters are ignored for Supervisor targets.
