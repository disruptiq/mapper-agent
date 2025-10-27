#!/usr/bin/env python3
import argparse
import concurrent.futures
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

processes = set()
processes_lock = threading.Lock()

def load_config(config_path):
    with open(config_path, 'r') as f:
        return json.load(f)

def install_dependencies(path, agent_name):
    # Try uv.lock first if uv is available
    uv_lock = os.path.join(path, 'uv.lock')
    if os.path.exists(uv_lock):
        try:
            subprocess.run(['uv', '--version'], check=True, capture_output=True)
            print(f"Installing dependencies for {agent_name} using uv...")
            subprocess.run(['uv', 'sync'], cwd=path, check=True)
            print(f"Dependencies installed for {agent_name} using uv")
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("uv not available, falling back to pip")

    # Try requirements.txt
    requirements_file = os.path.join(path, 'requirements.txt')
    if os.path.exists(requirements_file):
        print(f"Installing dependencies for {agent_name}...")
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', requirements_file], check=True)
        print(f"Dependencies installed for {agent_name}")
        return

    # Try pyproject.toml
    pyproject_file = os.path.join(path, 'pyproject.toml')
    if os.path.exists(pyproject_file):
        print(f"Installing dependencies for {agent_name} from pyproject.toml...")
        try:
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-e', '.'], cwd=path, check=True)
            print(f"Dependencies installed for {agent_name} from pyproject.toml")
        except subprocess.CalledProcessError:
            # If editable install fails, try regular install
            try:
                subprocess.run([sys.executable, '-m', 'pip', 'install', '.'], cwd=path, check=True)
                print(f"Dependencies installed for {agent_name} from pyproject.toml")
            except subprocess.CalledProcessError as e:
                print(f"Failed to install dependencies from pyproject.toml for {agent_name}: {e}", file=sys.stderr)
        return

    print(f"No dependency file found for {agent_name}")

def clone_repo_if_needed(agent):
    repo_url = agent.get('repo')
    path = agent.get('path')
    if not repo_url or not path:
        return
    if os.path.isdir(path):
        return  # Already exists

    # Check if git is available
    try:
        subprocess.run(['git', '--version'], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print("Git is not installed or not in PATH. Please install Git to clone repositories.", file=sys.stderr)
        sys.exit(1)

    print(f"Cloning {repo_url} to {path}...")
    try:
        # Ensure parent directory exists
        parent_dir = os.path.dirname(path)
        os.makedirs(parent_dir, exist_ok=True)
        # Clone the repo
        subprocess.run(['git', 'clone', repo_url, path], check=True)
        print(f"Successfully cloned {repo_url}")
        # Install dependencies using the robust function
        install_dependencies(path, agent['name'])
    except subprocess.CalledProcessError as e:
        print(f"Error cloning {repo_url}: {e}", file=sys.stderr)
        sys.exit(1)

def run_agent(agent, param=None, stop_event=None):
    if stop_event is None:
        stop_event = threading.Event()
    path = agent['path']
    script = agent['script']
    name = agent['name']

    # Make param absolute path
    if param:
        if os.path.basename(os.path.dirname(os.path.dirname(os.getcwd()))) == 'deepfenceai':
            param = os.path.abspath(os.path.join('../..', param))
        else:
            param = os.path.abspath(param)

    # Build the script command
    script_cmd = script
    if param:
        script_cmd += f" {param}"

    # Change to the agent directory
    cwd = os.path.abspath(path) 
    # print(f"Running {name} in directory: {cwd}")
    # print(f"Command: {script_cmd}")

    if not os.path.isdir(cwd):
        print(f"Error: Directory {cwd} does not exist for {name}", file=sys.stderr)
        return None

    try:
        # Run the script in the agent's directory
        process = subprocess.Popen(script_cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with processes_lock:
            processes.add(process)

        start_time = time.time()
        while True:
            if stop_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                with processes_lock:
                    processes.discard(process)
                print(f"{name} terminated due to interrupt", file=sys.stderr)
                return None

            retcode = process.poll()
            if retcode is not None:
                break

            if time.time() - start_time > 120:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                with processes_lock:
                    processes.discard(process)
                print(f"Timeout running {name}", file=sys.stderr)
                return None

            time.sleep(0.1)

        with processes_lock:
            processes.discard(process)

        stdout, stderr = process.communicate()
        if process.returncode != 0:
            print(f"Error running {name}: Return code {process.returncode}", file=sys.stderr)
            print(f"Stderr: {stderr}", file=sys.stderr)
            print(f"Stdout: {stdout}", file=sys.stderr)
            return None

        print(f"{name} completed successfully.")

        # Collate the output file
        script_output_file = agent.get('script-output')
        if not script_output_file:
            print(f"Warning: No script-output defined for {name}")
            return None

        output_file_name = agent.get('output')
        if not output_file_name:
            print(f"Warning: No output defined for {name}")
            return None

        if os.path.basename(os.path.dirname(os.path.dirname(os.getcwd()))) == 'deepfenceai':
            output_dir = '../../outputs/mapper-agent'
        else:
            output_dir = 'output'
        os.makedirs(output_dir, exist_ok=True)

        src = os.path.join(cwd, script_output_file)
        dst = os.path.join(output_dir, output_file_name)

        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"Copied {src} to {dst}")
            return dst
        else:
            print(f"Warning: Script output file {src} not found for {name}")
            return None
    except Exception as e:
        with processes_lock:
            if 'process' in locals() and process in processes:
                processes.discard(process)
        print(f"Error running {name}: {e}", file=sys.stderr)
        return None

def main():
    stop_event = threading.Event()
    parser = argparse.ArgumentParser(description="Run mapper agents")
    parser.add_argument("param", help="Directory to pass to all agents")
    args = parser.parse_args()
    param = args.param

    config_path = 'config.json'
    if not os.path.exists(config_path):
        print(f"Config file {config_path} not found", file=sys.stderr)
        sys.exit(1)

    config = load_config(config_path)
    report_data = {}

    # Clone repos if needed
    for agent in config['agents']:
        if not agent.get('name') or not agent.get('path'):
            continue
        clone_repo_if_needed(agent)

    if os.path.basename(os.path.dirname(os.path.dirname(os.getcwd()))) == 'deepfenceai':
        output_dir = '../../outputs/mapper-agent'
        # Note: Not archiving the main outputs folder
    else:
        output_dir = 'output'
        if os.path.exists(output_dir) and os.listdir(output_dir):
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_name = f"output_archive_{timestamp}"
            shutil.move(output_dir, archive_name)
            print(f"Existing output moved to {archive_name}")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = {}
            for agent in config['agents']:
                if not agent.get('name') or not agent.get('path'):
                    continue
                print(f"Running {agent['name']}...")
                futures[agent['name']] = executor.submit(run_agent, agent, param, stop_event)

            for agent in config['agents']:
                if not agent.get('name') or not agent.get('path'):
                    continue
                name = agent['name']
                output = futures[name].result()
                if output is not None:
                    report_data[name] = output

        # Save the report data to report.json
        with open('report.json', 'w') as f:
            json.dump(report_data, f, indent=2)
        print("Report saved to report.json")

    except KeyboardInterrupt:
        print("Interrupted by user. Terminating all processes...", file=sys.stderr)
        stop_event.set()
        with processes_lock:
            for p in list(processes):
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        # Wait a bit for processes to terminate
        time.sleep(1)
        sys.exit(1)

if __name__ == "__main__":
    main()
