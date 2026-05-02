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

    # 2. Fix permissions in data dir (common source of 'no page' errors)
    subprocess.run(["chown", "-R", "1000:1000", "/opt/hermes-data"], capture_output=True)

    # 3. Restart
    os.chdir(os.path.dirname(path))
    subprocess.run(["docker", "compose", "down"], capture_output=True)
    subprocess.run(["docker", "compose", "up", "-d"], capture_output=True)
    
    # 4. Open Firewall
    subprocess.run(["iptables", "-I", "INPUT", "-p", "tcp", "--dport", "3001", "-j", "ACCEPT"])
    print("SUCCESS: Full reset complete.")
except Exception as e:
    print(f"FAILED: {e}")
