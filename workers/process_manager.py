import os
import json
import re
import asyncio
import subprocess
import platform
import time
import atexit
import signal
import sys
import shlex
import uuid
import torch

from ..utils.logging import debug_log, log
from ..utils.config import load_config, save_config
from ..utils.process import is_process_alive, terminate_process, get_python_executable
from ..utils.constants import PROCESS_TERMINATION_TIMEOUT, WORKER_CHECK_INTERVAL, PROCESS_WAIT_TIMEOUT

# Try to import psutil for better process management
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    log("psutil not available, using fallback process management")
    PSUTIL_AVAILABLE = False


class WorkerProcessManager:
    def __init__(self):
        self.processes = {}  # worker_id -> process info
        self.load_processes()
        
    def find_comfy_root(self):
        """Find the ComfyUI root directory."""
        # Start from current file location
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Method 1: Check for environment variable override
        env_root = os.environ.get('COMFYUI_ROOT')
        if env_root and os.path.exists(os.path.join(env_root, "main.py")):
            debug_log(f"Found ComfyUI root via COMFYUI_ROOT environment variable: {env_root}")
            return env_root
        
        # Method 2: Try going up from custom_nodes directory
        # This file should be in ComfyUI/custom_nodes/ComfyUI-Distributed/
        potential_root = os.path.dirname(os.path.dirname(current_dir))
        if os.path.exists(os.path.join(potential_root, "main.py")):
            debug_log(f"Found ComfyUI root via directory traversal: {potential_root}")
            return potential_root
        
        # Method 3: Look for common Docker paths
        docker_paths = ["/basedir", "/ComfyUI", "/app", "/workspace/ComfyUI", "/comfyui", "/opt/ComfyUI", "/workspace"]
        for path in docker_paths:
            if os.path.exists(path) and os.path.exists(os.path.join(path, "main.py")):
                debug_log(f"Found ComfyUI root in Docker path: {path}")
                return path
        
        # Method 4: Search upwards for main.py
        search_dir = current_dir
        for _ in range(5):  # Limit search depth
            if os.path.exists(os.path.join(search_dir, "main.py")):
                debug_log(f"Found ComfyUI root via upward search: {search_dir}")
                return search_dir
            parent = os.path.dirname(search_dir)
            if parent == search_dir:  # Reached root
                break
            search_dir = parent
        
        # Method 5: Try to import and use folder_paths
        try:
            import folder_paths
            # folder_paths.base_path should point to ComfyUI root
            if hasattr(folder_paths, 'base_path') and os.path.exists(os.path.join(folder_paths.base_path, "main.py")):
                debug_log(f"Found ComfyUI root via folder_paths: {folder_paths.base_path}")
                return folder_paths.base_path
        except:
            pass
        
        # If all methods fail, log detailed information and return the best guess
        log(f"Warning: Could not reliably determine ComfyUI root directory")
        log(f"Current directory: {current_dir}")
        log(f"Initial guess was: {potential_root}")
        
        # Return the initial guess as fallback
        return potential_root
        
    def _find_windows_terminal(self):
        """Find Windows Terminal executable."""
        # Common locations for Windows Terminal
        possible_paths = [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\WindowsApps\Microsoft.WindowsTerminal_*\wt.exe"),
            "wt.exe"  # Try PATH
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                return path
            # Handle wildcard for WindowsApps
            if '*' in path:
                import glob
                matches = glob.glob(path)
                if matches:
                    return matches[0]
        
        # Try to find it in PATH
        import shutil
        wt_path = shutil.which("wt")
        if wt_path:
            return wt_path
            
        return None
        
    def build_launch_command(self, worker_config, comfy_root):
        """Build the command to launch a worker."""
        # Use main.py directly - it's the most reliable method
        main_py = os.path.join(comfy_root, "main.py")
        
        if os.path.exists(main_py):
            cmd = [
                get_python_executable(),
                main_py,
                "--port", str(worker_config['port']),
                "--enable-cors-header"
            ]
            debug_log(f"Using main.py: {main_py}")
        else:
            # Provide detailed error message
            error_msg = f"Could not find main.py in {comfy_root}\n"
            error_msg += f"Searched for: {main_py}\n"
            error_msg += f"Directory contents of {comfy_root}:\n"
            try:
                if os.path.exists(comfy_root):
                    files = os.listdir(comfy_root)[:20]  # List first 20 files
                    error_msg += "  " + "\n  ".join(files)
                    if len(os.listdir(comfy_root)) > 20:
                        error_msg += f"\n  ... and {len(os.listdir(comfy_root)) - 20} more files"
                else:
                    error_msg += f"  Directory {comfy_root} does not exist!"
            except Exception as e:
                error_msg += f"  Error listing directory: {e}"
            
            # Try to suggest the correct path
            error_msg += "\n\nPossible solutions:\n"
            error_msg += "1. Check if ComfyUI is installed in a different location\n"
            error_msg += "2. For Docker: ComfyUI might be in /ComfyUI or /app\n"
            error_msg += "3. Ensure the custom node is installed in the correct location\n"
            
            raise RuntimeError(error_msg)
        
        # Add any extra arguments safely
        if worker_config.get('extra_args'):
            raw_args = worker_config['extra_args'].strip()
            if raw_args:
                # Safely split using shlex to handle quotes and spaces
                extra_args_list = shlex.split(raw_args)
                
                # Validate: Block dangerous shell meta-characters (e.g., ; | & > < ` $)
                forbidden_chars = set(';|>&<`$()[]{}*!?')
                for arg in extra_args_list:
                    if any(c in forbidden_chars for c in arg):
                        raise ValueError(f"Invalid characters in extra_args: {arg}. Forbidden: {''.join(forbidden_chars)}")
                
                cmd.extend(extra_args_list)
            
        return cmd
        
    def launch_worker(self, worker_config, show_window=False):
        """Launch a worker process with logging."""
        comfy_root = self.find_comfy_root()
        
        # Set up environment
        env = os.environ.copy()
        env['CUDA_VISIBLE_DEVICES'] = str(worker_config.get('cuda_device', 0))
        env['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
        
        # Pass master PID to worker so it can monitor if master is still alive
        env['COMFYUI_MASTER_PID'] = str(os.getpid())
        
        cmd = self.build_launch_command(worker_config, comfy_root)
        
        # Change to ComfyUI root directory for the process
        cwd = comfy_root
        
        # Create log directory and file
        log_dir = os.path.join(comfy_root, "logs", "workers")
        os.makedirs(log_dir, exist_ok=True)
        
        # Use daily log files instead of timestamp
        date_stamp = time.strftime("%Y%m%d")
        worker_name = worker_config.get('name', f'Worker{worker_config["id"]}')
        # Clean worker name for filename
        safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in worker_name)
        log_file = os.path.join(log_dir, f"{safe_name}_{date_stamp}.log")
        
        # Launch process with logging (append mode for daily logs)
        with open(log_file, 'a') as log_handle:
            # Write startup info to log with timestamp
            log_handle.write(f"\n\n{'='*50}\n")
            log_handle.write(f"=== ComfyUI Worker Session Started ===\n")
            log_handle.write(f"Worker: {worker_name}\n")
            log_handle.write(f"Port: {worker_config['port']}\n")
            log_handle.write(f"CUDA Device: {worker_config.get('cuda_device', 0)}\n")
            log_handle.write(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_handle.write(f"Command: {' '.join(cmd)}\n")
            
            # Note about worker behavior
            config = load_config()
            stop_on_master_exit = config.get('settings', {}).get('stop_workers_on_master_exit', True)
            
            if stop_on_master_exit:
                log_handle.write("Note: Worker will stop when master shuts down\n")
            else:
                log_handle.write("Note: Worker will continue running after master shuts down\n")
            
            log_handle.write("=" * 30 + "\n\n")
            log_handle.flush()
            
            # Wrap command with monitor if needed
            if stop_on_master_exit and env.get('COMFYUI_MASTER_PID'):
                # Use the monitor wrapper
                monitor_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'worker_monitor.py')
                monitored_cmd = [get_python_executable(), monitor_script] + cmd
                log_handle.write(f"[Worker Monitor] Monitoring master PID: {env['COMFYUI_MASTER_PID']}\n")
                log_handle.flush()
            else:
                monitored_cmd = cmd
            
            # Platform-specific process creation - always hidden with logging
            if platform.system() == "Windows":
                CREATE_NO_WINDOW = 0x08000000
                process = subprocess.Popen(
                    monitored_cmd, env=env, cwd=cwd,
                    stdout=log_handle, 
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW
                )
            else:
                # Unix-like systems
                process = subprocess.Popen(
                    monitored_cmd, env=env, cwd=cwd,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True  # Detach from parent
                )
        
        # Track the process with log file info - use string ID for consistency
        worker_id = str(worker_config['id'])
        self.processes[worker_id] = {
            'pid': process.pid,
            'process': process,
            'started_at': time.time(),
            'config': worker_config,
            'log_file': log_file,
            'is_monitor': stop_on_master_exit and env.get('COMFYUI_MASTER_PID'),  # Track if using monitor
            'launching': True  # Mark as launching until confirmed running
        }
        
        # Save process info for persistence
        self.save_processes()
        
        if stop_on_master_exit and env.get('COMFYUI_MASTER_PID'):
            debug_log(f"Launched worker {worker_name} via monitor (Monitor PID: {process.pid})")
        else:
            log(f"Launched worker {worker_name} directly (PID: {process.pid})")
        debug_log(f"Log file: {log_file}")
        return process.pid
        
    def stop_worker(self, worker_id):
        """Stop a worker process."""
        # Ensure worker_id is string
        worker_id = str(worker_id)
        if worker_id not in self.processes:
            return False, "Worker not managed by UI"
            
        proc_info = self.processes[worker_id]
        process = proc_info.get('process')
        pid = proc_info['pid']
        
        debug_log(f"Attempting to stop worker {worker_id} (PID: {pid})")
        
        # For restored processes without subprocess object
        if not process:
            try:
                print(f"[Distributed] Stopping restored process (no subprocess object)")
                if self._kill_process_tree(pid):
                    del self.processes[worker_id]
                    self.save_processes()
                    debug_log(f"Successfully stopped worker {worker_id} and all child processes")
                    return True, "Worker stopped"
                else:
                    return False, "Failed to stop worker process"
            except Exception as e:
                print(f"[MultiGPU] Exception during stop: {e}")
                return False, f"Error stopping worker: {str(e)}"
        
        # Normal case with subprocess object
        # Check if still running
        if process.poll() is not None:
            # Already stopped
            print(f"[Distributed] Worker {worker_id} already stopped")
            del self.processes[worker_id]
            self.save_processes()
            return False, "Worker already stopped"
            
        # Try to kill the entire process tree
        try:
            debug_log(f"Using process tree kill for worker {worker_id}")
            if self._kill_process_tree(pid):
                # Clean up tracking
                del self.processes[worker_id]
                self.save_processes()
                debug_log(f"Successfully stopped worker {worker_id} and all child processes")
                return True, "Worker stopped"
            else:
                # Fallback to normal termination
                print(f"[Distributed] Process tree kill failed, trying normal termination")
                if process:
                    terminate_process(process, timeout=PROCESS_TERMINATION_TIMEOUT)
                
                del self.processes[worker_id]
                self.save_processes()
                return True, "Worker stopped (fallback)"
                
        except Exception as e:
            print(f"[Distributed] Exception during stop: {e}")
            return False, f"Error stopping worker: {str(e)}"
            
    def get_managed_workers(self):
        """Get list of workers managed by this process."""
        managed = {}
        for worker_id, proc_info in list(self.processes.items()):
            # Check if process is still running
            is_running, _ = self._check_worker_process(worker_id, proc_info)
            
            if is_running:
                managed[worker_id] = {
                    'pid': proc_info['pid'],
                    'started_at': proc_info['started_at'],
                    'log_file': proc_info.get('log_file'),
                    'launching': proc_info.get('launching', False)
                }
            else:
                # Process has stopped, remove from tracking
                del self.processes[worker_id]
        
        return managed
        
    def cleanup_all(self):
        """Stop all managed workers (called on shutdown)."""
        for worker_id in list(self.processes.keys()):
            try:
                self.stop_worker(worker_id)
            except Exception as e:
                print(f"[Distributed] Error stopping worker {worker_id}: {e}")
        
        # Clear all managed processes from config
        config = load_config()
        config['managed_processes'] = {}
        save_config(config)
    
    def load_processes(self):
        """Load persisted process information from config."""
        config = load_config()
        managed_processes = config.get('managed_processes', {})
        
        # Verify each saved process is still running
        for worker_id, proc_info in managed_processes.items():
            pid = proc_info.get('pid')
            if pid and self._is_process_running(pid):
                # Reconstruct process info
                self.processes[worker_id] = {
                    'pid': pid,
                    'process': None,  # Can't reconstruct subprocess object
                    'started_at': proc_info.get('started_at'),
                    'config': proc_info.get('config'),
                    'log_file': proc_info.get('log_file')
                }
                print(f"[Distributed] Restored worker {worker_id} (PID: {pid})")
            else:
                if pid:
                    print(f"[Distributed] Worker {worker_id} (PID: {pid}) is no longer running")
    
    def save_processes(self):
        """Save process information to config."""
        config = load_config()
        
        # Create serializable version of process info
        managed_processes = {}
        for worker_id, proc_info in self.processes.items():
            # Only save if process is running
            is_running, _ = self._check_worker_process(worker_id, proc_info)
            
            if is_running:
                managed_processes[worker_id] = {
                    'pid': proc_info['pid'],
                    'started_at': proc_info['started_at'],
                    'config': proc_info['config'],
                    'log_file': proc_info.get('log_file'),
                    'launching': proc_info.get('launching', False)
                }
        
        # Update config with managed processes
        config['managed_processes'] = managed_processes
        save_config(config)
    
    def _is_process_running(self, pid):
        """Check if a process with given PID is running."""
        return is_process_alive(pid)
    
    def _check_worker_process(self, worker_id, proc_info):
        """Check if a worker process is still running and return status.
        
        Returns:
            tuple: (is_running, has_subprocess_object)
        """
        process = proc_info.get('process')
        pid = proc_info.get('pid')
        
        if process:
            # Normal case with subprocess object
            return process.poll() is None, True
        elif pid:
            # Restored process without subprocess object
            return self._is_process_running(pid), False
        else:
            # No process or PID
            return False, False
    
    def _kill_process_tree(self, pid):
        """Kill a process and all its children."""
        if PSUTIL_AVAILABLE:
            try:
                parent = psutil.Process(pid)
                children = parent.children(recursive=True)
                
                # Log what we're about to kill
                debug_log(f"Killing process tree for PID {pid} ({parent.name()})")
                for child in children:
                    debug_log(f"  - Child PID {child.pid} ({child.name()})")
                
                # Kill children first
                for child in children:
                    try:
                        debug_log(f"Terminating child {child.pid}")
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                
                # Wait a bit for graceful termination
                gone, alive = psutil.wait_procs(children, timeout=PROCESS_WAIT_TIMEOUT)
                
                # Force kill any remaining
                for child in alive:
                    try:
                        debug_log(f"Force killing child {child.pid}")
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                
                # Finally kill the parent
                try:
                    debug_log(f"Terminating parent {pid}")
                    parent.terminate()
                    parent.wait(timeout=PROCESS_WAIT_TIMEOUT)
                except psutil.TimeoutExpired:
                    debug_log(f"Force killing parent {pid}")
                    parent.kill()
                except psutil.NoSuchProcess:
                    debug_log(f"Parent process {pid} already gone")
                    
                return True
                
            except psutil.NoSuchProcess:
                debug_log(f"Process {pid} does not exist")
                return False
            except Exception as e:
                debug_log(f"Error killing process tree: {e}")
                # Fall through to OS commands
        
        # Fallback to OS-specific commands
        print(f"[Distributed] Using OS commands to kill process tree")
        if platform.system() == "Windows":
            try:
                # Use wmic to find child processes
                result = subprocess.run(['wmic', 'process', 'where', f'ParentProcessId={pid}', 'get', 'ProcessId'], 
                                      capture_output=True, text=True)
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')[1:]  # Skip header
                    child_pids = [line.strip() for line in lines if line.strip() and line.strip().isdigit()]
                    
                    print(f"[Distributed] Found child processes: {child_pids}")
                    
                    # Kill each child
                    for child_pid in child_pids:
                        try:
                            subprocess.run(['taskkill', '/F', '/PID', child_pid], 
                                         capture_output=True, check=False)
                        except:
                            pass
                
                # Kill the parent with tree flag
                result = subprocess.run(['taskkill', '/F', '/PID', str(pid), '/T'], 
                                      capture_output=True, text=True)
                print(f"[Distributed] Taskkill result: {result.stdout.strip()}")
                return result.returncode == 0
            except Exception as e:
                print(f"[Distributed] Error with taskkill: {e}")
                return False
        else:
            # Unix: use pkill
            try:
                subprocess.run(['pkill', '-TERM', '-P', str(pid)], check=False)
                time.sleep(WORKER_CHECK_INTERVAL)
                subprocess.run(['pkill', '-KILL', '-P', str(pid)], check=False)
                os.kill(pid, signal.SIGKILL)
                return True
            except:
                return False
