import os, subprocess, sys

def fix():
    # 1. Find Compose
    path = subprocess.check_output("find / -name docker-compose.yml 2>/dev/null | xargs grep -l 'hermes' | head -n 1", shell=True).decode().strip()
    if not path:
        print("Error: No compose found")
        return

    print(f"Fixing {path}")
    with open(path, 'r') as f: lines = f.readlines()

    new_lines = []
    in_hermes = False
    ports_added = False
    env_added = False

    for line in lines:
        if line.strip().startswith('hermes:'):
            in_hermes = True
            new_lines.append(line)
            continue
        
        if in_hermes:
            # Detect exit from hermes service block
            if line.strip() and not line.startswith('  ') and not line.startswith(' '):
                in_hermes = False
            
            # Inject Ports
            if in_hermes and not ports_added and ('image:' in line or 'container_name:' in line):
                new_lines.append(line)
                new_lines.append('    ports:\n')
                new_lines.append('      - "3001:3000"\n')
                ports_added = True
                continue

            # Inject Environment
            if in_hermes and not env_added and ('volumes:' in line):
                new_lines.append('    environment:\n')
                new_lines.append('      - HOST=0.0.0.0\n')
                new_lines.append('      - PORT=3000\n')
                new_lines.append(line)
                env_added = True
                continue

        # Prevent duplicate entries
        if '3001:3000' in line or 'HOST=0.0.0.0' in line or 'PORT=3000' in line:
            continue
        new_lines.append(line)

    with open(path, 'w') as f: f.writelines(new_lines)

    # 2. Restart
    os.chdir(os.path.dirname(path))
    subprocess.run(["docker", "compose", "up", "-d"])
    subprocess.run(["iptables", "-I", "INPUT", "-p", "tcp", "--dport", "3001", "-j", "ACCEPT"])
    print("SUCCESS")

if __name__ == "__main__":
    fix()
