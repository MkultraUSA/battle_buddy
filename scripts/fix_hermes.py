import os, subprocess
try:
    # Find the compose file
    path = subprocess.check_output("find /opt -name docker-compose.yml 2>/dev/null | xargs grep -l 'hermes' | head -n 1", shell=True).decode().strip()
    if not path:
        path = subprocess.check_output("find / -name docker-compose.yml 2>/dev/null | xargs grep -l 'hermes' | head -n 1", shell=True).decode().strip()
    
    if not path:
        print("COULD NOT FIND COMPOSE FILE")
        exit(1)

    print(f"Found compose file: {path}")
    with open(path, 'r') as f: lines = f.readlines()
    
    new_lines = []
    in_hermes = False
    for line in lines:
        if line.strip().startswith('hermes:'): in_hermes = True
        elif in_hermes and line.strip() and not line.startswith(' ') and not line.strip().startswith('hermes:'): 
            # Check if this is a new top-level service
            if not line.startswith('  '): 
                in_hermes = False
        
        if in_hermes and 'ports:' in line:
            new_lines.append(line)
            # Add port 3001 mapping
            new_lines.append('      - "3001:3000"\n')
            continue
            
        if '3001:3000' in line: continue
        new_lines.append(line)
        
    with open(path, 'w') as f: f.writelines(new_lines)
    
    # Restart
    os.chdir(os.path.dirname(path))
    subprocess.run(["docker", "compose", "up", "-d"])
    # Firewall
    subprocess.run(["iptables", "-I", "INPUT", "-p", "tcp", "--dport", "3001", "-j", "ACCEPT"])
    print("SUCCESS: Hermes should be on 3001")
except Exception as e:
    print(f"FAILED: {e}")
