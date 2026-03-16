# Implementation Plan: PM2 Integration

## Overview

Add PM2 process manager support to Superscaler with a minimal approach: `type` field in target config, dispatch in scaler, PM2 CLI via subprocess. No abstract base class, no engine refactor. Implementation follows dependency order: config → pm2_client → scaler → main → service file → version bump.

## Tasks

- [x] 1. Update config.py — Add PM2 fields to TargetConfig and update load_config
  - [x] 1.1 Add `type`, `pm2_path`, `pm2_home`, `run_as_user` fields to TargetConfig dataclass
    - Add `type: str` field (after `name`) with default `'supervisor'`
    - Add `pm2_path: str`, `pm2_home: str`, `run_as_user: str` fields with default `''`
    - _Requirements: 1.4, 9.4_

  - [x] 1.2 Update load_config to parse new target fields and validate type
    - Parse optional `type` field from each `[target:*]` section, default to `'supervisor'`
    - Raise `ValueError` if `type` is not `supervisor` or `pm2`
    - Parse optional `pm2_path`, `pm2_home`, `run_as_user` as strings, default to `''`
    - Log warning if PM2 fields are set on a supervisor target
    - _Requirements: 1.1, 1.2, 1.3, 9.1, 9.2, 9.3, 9.11_

  - [x] 1.3 Relax `[supervisor]` section requirement — only required when supervisor targets exist
    - Move `[supervisor]` section parsing after target sections are parsed
    - Only require `[supervisor]` section if at least one target has `type = supervisor`
    - Update `SuperscalerConfig` to allow `unix_socket_path`, `sv_username`, `sv_password` to be empty strings when no supervisor targets exist
    - _Requirements: 2.3_

  - [ ]* 1.4 Write property tests for config type field validation (test_config_pm2.py)
    - **Property 1: Config type field validation**
    - **Validates: Requirements 1.1, 1.2, 1.3**

  - [ ]* 1.5 Write property tests for PM2 optional config fields parsing (test_config_pm2.py)
    - **Property 2: PM2 optional config fields parsing**
    - **Validates: Requirements 9.1, 9.2, 9.3, 9.4**

  - [ ]* 1.6 Write property test for supervisor section conditional requirement (test_config_pm2.py)
    - **Property 12: Supervisor section required only when supervisor targets exist**
    - **Validates: Requirements 2.3**

  - [ ]* 1.7 Write property test for warning on PM2 fields on supervisor targets (test_config_pm2.py)
    - **Property 10: Warning for PM2 fields on supervisor targets**
    - **Validates: Requirements 9.11**

  - [ ]* 1.8 Write unit tests for config PM2 parsing edge cases (test_config_pm2.py)
    - Test specific config file examples: PM2-only, mixed targets, supervisor-only
    - Test invalid type value raises ValueError with supported types listed
    - Test PM2 fields ignored warning for supervisor targets
    - _Requirements: 1.1, 1.2, 1.3, 9.11_

- [x] 2. Checkpoint — Ensure config changes are solid
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Create pm2_client.py — PM2 CLI helper module
  - [x] 3.1 Create `src/superscaler/pm2_client.py` with `_build_pm2_cmd` and `PM2_STATUS_MAP`
    - Define `PM2_TIMEOUT = 30` constant
    - Define `PM2_STATUS_MAP` dict mapping PM2 statuses to ScalerEngine statenames
    - Implement `_build_pm2_cmd(args, pm2_path, pm2_home, run_as_user)` returning `(cmd, env)` tuple
    - When `pm2_path` is non-empty, use it as binary; otherwise use `'pm2'`
    - When `run_as_user` is non-empty, prefix command with `sudo -u <run_as_user>`
    - When `pm2_home` is non-empty, set `PM2_HOME` in env dict
    - _Requirements: 9.5, 9.6, 9.7, 9.8_

  - [x] 3.2 Implement `pm2_ping` function
    - Execute `pm2 ping` via `subprocess.run` with timeout
    - Return `True` if exit code is 0, `False` otherwise
    - _Requirements: 6.1, 8.6_

  - [x] 3.3 Implement `pm2_get_group_info` function
    - Execute `pm2 jlist` via `subprocess.run` with timeout
    - Parse JSON output, filter by `name == program_name`
    - Map PM2 statuses using `PM2_STATUS_MAP`, unknown statuses map to `'UNKNOWN'`
    - Return dict with `count` and `processes` keys matching SupervisorClient format
    - Raise on non-zero exit code
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 8.4_

  - [x] 3.4 Implement `pm2_scale_up` function
    - Get process list before via `pm2 jlist`
    - Execute `pm2 scale <program_name> +<count>` via `subprocess.run` with timeout
    - Get process list after via `pm2 jlist`
    - Return list of newly added process names by diffing before/after
    - Raise on non-zero exit code
    - _Requirements: 3.1, 8.5_

  - [x] 3.5 Implement `pm2_scale_down` function
    - Execute `pm2 scale <program_name> <desired_count>` via `subprocess.run` with timeout
    - Raise on non-zero exit code
    - _Requirements: 4.1_

  - [ ]* 3.6 Write property test for PM2 command building (test_pm2_client.py)
    - **Property 3: PM2 command building**
    - **Validates: Requirements 3.1, 4.1, 9.5, 9.6, 9.7, 9.8**

  - [ ]* 3.7 Write property test for pm2_get_group_info parsing and filtering (test_pm2_client.py)
    - **Property 4: pm2_get_group_info parsing and filtering**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 8.4**

  - [ ]* 3.8 Write property test for PM2 subprocess non-zero exit raises exception (test_pm2_client.py)
    - **Property 5: PM2 subprocess non-zero exit raises exception**
    - **Validates: Requirements 3.2, 4.3, 5.5**

  - [ ]* 3.9 Write property test for scale up diff (test_pm2_client.py)
    - **Property 8: Scale up returns added process names via diff**
    - **Validates: Requirements 8.5**

  - [ ]* 3.10 Write property test for pm2_ping exit code mapping (test_pm2_client.py)
    - **Property 9: PM2 ping maps exit code to boolean**
    - **Validates: Requirements 8.6**

  - [ ]* 3.11 Write unit tests for pm2_client edge cases (test_pm2_client.py)
    - Test unknown PM2 status maps to `UNKNOWN`
    - Test empty jlist output returns count=0 and empty processes list
    - Test subprocess timeout raises `subprocess.TimeoutExpired`
    - _Requirements: 5.3, 8.2, 8.3_

- [x] 4. Checkpoint — Ensure pm2_client is solid
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Update scaler.py — Add PM2 target dispatch
  - [x] 5.1 Rename existing `_process_target` to `_process_supervisor_target`
    - Move all existing logic from `_process_target` into `_process_supervisor_target` unchanged
    - _Requirements: 2.1, 2.2_

  - [x] 5.2 Create new `_process_target` as dispatcher
    - Check `target.type` field
    - If `pm2`, call `_process_pm2_target`
    - Otherwise, call `_process_supervisor_target`
    - _Requirements: 7.1_

  - [x] 5.3 Implement `_process_pm2_target` method
    - Import and call `pm2_get_group_info`, `pm2_scale_up`, `pm2_scale_down` from pm2_client
    - Pass `target.pm2_path`, `target.pm2_home`, `target.run_as_user` to all pm2_client calls
    - Use same desired worker calculation: `ceil(queue_len / tasks_per_worker)`, bounded by min/max
    - Use same cooldown logic: check `last_up`/`last_down` timers
    - Use same step limits: `scale_up_step`, `scale_down_step`
    - Skip pending state tracking entirely — no `pending` list usage
    - Count active workers using `ACTIVE_STATES` from pm2_get_group_info result
    - Log errors and skip tick on pm2_client exceptions
    - _Requirements: 3.1, 3.2, 4.1, 4.2, 4.3, 5.5, 7.3, 7.4_

  - [ ]* 5.4 Write property test for no pending state tracking for PM2 targets (test_scaler_pm2.py)
    - **Property 6: No pending state tracking for PM2 targets**
    - **Validates: Requirements 4.2, 7.3**

  - [ ]* 5.5 Write property test for scaling logic preserved for both target types (test_scaler_pm2.py)
    - **Property 7: Scaling logic preserved for both target types**
    - **Validates: Requirements 7.4**

  - [ ]* 5.6 Write property test for supervisor targets use SupervisorClient with pending tracking (test_scaler_pm2.py)
    - **Property 11: Supervisor targets use SupervisorClient with pending tracking**
    - **Validates: Requirements 2.1, 2.2, 7.2**

  - [ ]* 5.7 Write unit tests for scaler PM2 dispatch (test_scaler_pm2.py)
    - Test PM2 target calls pm2_client functions, not SupervisorClient
    - Test supervisor target still calls SupervisorClient methods
    - Test mixed targets in same engine tick
    - Test pm2_get_group_info failure skips tick
    - Test pm2_scale_up failure logs error and continues
    - _Requirements: 7.1, 7.2, 7.3_

- [x] 6. Checkpoint — Ensure scaler dispatch works
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Update main.py — Conditional startup checks
  - [x] 7.1 Make supervisor client creation and ping conditional
    - Only create `SupervisorClient` and ping if `has_supervisor_targets` is True
    - Set `sv_client = None` when no supervisor targets exist
    - _Requirements: 2.3_

  - [x] 7.2 Add PM2 startup checks
    - When `has_pm2_targets` is True, verify `pm2_path` exists and is executable for targets that set it
    - Ping PM2 using first PM2 target's settings via `pm2_ping`
    - Exit with code 1 if `pm2_path` validation or ping fails
    - _Requirements: 6.1, 6.2, 9.9, 9.10_

  - [ ]* 7.3 Write unit tests for main.py PM2 startup checks (test_main_pm2.py)
    - Test pm2_ping called when PM2 targets exist
    - Test pm2_ping NOT called when only supervisor targets exist
    - Test pm2_path validation: missing file exits with code 1
    - Test pm2_path validation: non-executable file exits with code 1
    - Test supervisor client NOT created when only PM2 targets exist
    - _Requirements: 6.1, 6.2, 9.9, 9.10_

- [x] 8. Update packaging/superscaler.service — Remove supervisor dependency
  - Change `After=network.target supervisord.service supervisor.service` to `After=network.target`
  - Update `Description` to `Superscaler for autoscaling worker processes`
  - _Requirements: (service must work standalone regardless of process manager)_

- [x] 9. Update packaging/superscaler.conf — Add PM2 target example
  - Add `type = supervisor` to existing example target section
  - Add commented-out PM2 target example section with `type = pm2`, `pm2_path`, `pm2_home`, `run_as_user` fields
  - _Requirements: 1.1, 9.1, 9.2, 9.3_

- [x] 10. Version bump — Update version from 2.1.3 to 2.2.0 across all files
  - Update `version` in `pyproject.toml`
  - Update version references in `README.md` (download URLs and install commands)
  - Add new entry in `packaging/deb/changelog`
  - Update `Version` in `packaging/rpm/superscaler.spec` and add changelog entry
  - _Requirements: (project convention — every code change must bump version)_

- [x] 11. Final checkpoint — Ensure everything is wired together
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- Implementation language: Python (matching existing codebase)
- Test framework: hypothesis for property-based tests, pytest for unit tests
