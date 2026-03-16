"""PM2 CLI helper module.

Provides functions for interacting with PM2 process manager via subprocess.
All functions accept execution environment parameters (pm2_path, pm2_home,
run_as_user) to build the correct subprocess command.
"""

import json
import logging
import os
import subprocess

logger = logging.getLogger('superscaler')

PM2_TIMEOUT = 30

PM2_STATUS_MAP = {
    'online': 'RUNNING',
    'stopping': 'STOPPING',
    'stopped': 'STOPPED',
    'errored': 'FATAL',
    'launching': 'STARTING',
}

PM2_STATE_CODES = {
    'RUNNING': 20,
    'STARTING': 10,
    'STOPPING': 40,
    'STOPPED': 0,
    'FATAL': 200,
    'UNKNOWN': -1,
}


def _build_pm2_cmd(args, pm2_path='', pm2_home='', run_as_user=''):
    """Build the subprocess command list and env dict for a PM2 call.

    Returns (cmd, env) where cmd is the full command list and env is
    the environment dict (or None if no modifications needed).
    """
    binary = pm2_path if pm2_path else 'pm2'
    cmd = [binary] + list(args)

    if run_as_user:
        cmd = ['sudo', '-u', run_as_user] + cmd

    env = None
    if pm2_home:
        env = os.environ.copy()
        env['PM2_HOME'] = pm2_home

    return (cmd, env)


def pm2_ping(pm2_path='', pm2_home='', run_as_user=''):
    """Return True if PM2 daemon is reachable (exit code 0)."""
    cmd, env = _build_pm2_cmd(['ping'], pm2_path, pm2_home, run_as_user)
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, timeout=PM2_TIMEOUT)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def pm2_get_group_info(program_name, pm2_path='', pm2_home='', run_as_user=''):
    """Run ``pm2 jlist``, filter by *program_name*, return dict matching
    SupervisorClient.get_group_info format:

    {'count': int,
     'processes': [{'name': str, 'pid': int, 'state': int, 'statename': str}, ...]}

    Raises on non-zero exit code.
    """
    cmd, env = _build_pm2_cmd(['jlist'], pm2_path, pm2_home, run_as_user)
    result = subprocess.run(cmd, env=env, capture_output=True, timeout=PM2_TIMEOUT)
    result.check_returncode()

    all_procs = json.loads(result.stdout)
    processes = []
    for proc in all_procs:
        if proc.get('name') != program_name:
            continue
        pm2_status = proc.get('pm2_env', {}).get('status', '')
        statename = PM2_STATUS_MAP.get(pm2_status, 'UNKNOWN')
        state = PM2_STATE_CODES[statename]
        processes.append({
            'name': proc.get('name'),
            'pid': proc.get('pid', 0),
            'state': state,
            'statename': statename,
        })

    return {
        'count': len(processes),
        'processes': processes,
    }

def pm2_scale_up(program_name, count, pm2_path='', pm2_home='', run_as_user=''):
    """Run ``pm2 scale <program_name> +<count>``.
    Returns list of newly added process names by diffing jlist before/after.
    Raises on non-zero exit code.
    """
    # Get before state
    cmd, env = _build_pm2_cmd(['jlist'], pm2_path, pm2_home, run_as_user)
    before = subprocess.run(cmd, env=env, capture_output=True, timeout=PM2_TIMEOUT)
    before.check_returncode()
    before_procs = json.loads(before.stdout)
    before_ids = {p['pm_id'] for p in before_procs if p.get('name') == program_name}

    # Scale up
    cmd, env = _build_pm2_cmd(['scale', program_name, '+%d' % count], pm2_path, pm2_home, run_as_user)
    result = subprocess.run(cmd, env=env, capture_output=True, timeout=PM2_TIMEOUT)
    result.check_returncode()

    # Get after state
    cmd, env = _build_pm2_cmd(['jlist'], pm2_path, pm2_home, run_as_user)
    after = subprocess.run(cmd, env=env, capture_output=True, timeout=PM2_TIMEOUT)
    after.check_returncode()
    after_procs = json.loads(after.stdout)

    added = []
    for p in after_procs:
        if p.get('name') == program_name and p['pm_id'] not in before_ids:
            added.append('%s-%d' % (program_name, p['pm_id']))
    return added

def pm2_scale_down(program_name, desired_count, pm2_path='', pm2_home='', run_as_user=''):
    """Run ``pm2 scale <program_name> <desired_count>``.
    Raises on non-zero exit code.
    """
    cmd, env = _build_pm2_cmd(['scale', program_name, str(desired_count)], pm2_path, pm2_home, run_as_user)
    result = subprocess.run(cmd, env=env, capture_output=True, timeout=PM2_TIMEOUT)
    result.check_returncode()


