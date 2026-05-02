import os, subprocess
try:
    # 1. Find and Fix Docker Compose
    path = subprocess.check_output("find /opt -name docker-compose.yml 2>/dev/null | xargs grep -l 'hermes' | head -n 1", shell=True).decode().strip()
    if not path:
        path = subprocess.check_output("find / -name docker-compose.yml 2>/dev/null | xargs grep -l 'hermes' | head -n 1", shell=True).decode().strip()
    
    if path:
        with open(path, 'r') as f: lines = f.readlines()
        new_lines = []
        in_hermes = False
        for line in lines:
            if line.strip().startswith('hermes:'): in_hermes = True
            elif in_hermes and line.strip() and not line.startswith(' ') and not line.strip().startswith('hermes:'):
                if not line.startswith('  '): in_hermes = False
            if in_hermes and 'ports:' in line:
                new_lines.append(line)
                new_lines.append('      - "3001:3000"\n')
                continue
            if '3001:3000' in line: continue
            new_lines.append(line)
        with open(path, 'w') as f: f.writelines(new_lines)

    # 2. WIPE LOCKS AND SESSIONS (Crucial for fixing 'Accepted but no bytes' errors)
    print("Wiping locks and sessions...")
    data_dir = "/opt/hermes-data"
    subprocess.run(["rm", "-rf", os.path.join(data_dir, "sessions")], capture_output=True)
    subprocess.run(["rm", "-f", os.path.join(data_dir, "gateway.lock")], capture_output=True)
    subprocess.run(["chown", "-R", "1000:1000", data_dir], capture_output=True)

    # 3. RESTART
    print("Restarting Docker...")
    os.chdir(os.path.dirname(path))
    subprocess.run(["docker", "compose", "down"], capture_output=True)
    subprocess.run(["docker", "compose", "up", "-d"], capture_output=True)
    
    # 4. FIREWALL
    subprocess.run(["iptables", "-I", "INPUT", "-p", "tcp", "--dport", "3001", "-j", "ACCEPT"])
    
    # 5. POST-START LOG CHECK
    import time
    time.sleep(10)
    logs = subprocess.check_output(["docker", "logs", "--tail", "20", "hermes"]).decode()
    print("--- CONTAINER LOGS ---")
    print(logs)
    
    print("SUCCESS: State wiped and container restarted.")
except Exception as e:
    print(f"FAILED: {e}")
