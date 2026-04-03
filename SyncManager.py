import os
import subprocess
import time
import sys
import requests
import logging
import io
import traceback
import re
import json
import shutil
from datetime import datetime
from dotenv import load_dotenv

# --- INITIALIZATION ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# Load configuration
try:
    with open(CONFIG_PATH, 'r') as f:
        config_data = json.load(f)
        SYNC_TASKS = config_data.get("tasks",[])
except FileNotFoundError:
    print(f"CRITICAL: config.json not found at {CONFIG_PATH}. Please copy config.json.example and configure it.")
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"CRITICAL: config.json is improperly formatted: {e}")
    sys.exit(1)

if not SYNC_TASKS:
    print("WARNING: No sync tasks found in config.json. Exiting.")
    sys.exit(0)

# --- LOGGING INFRASTRUCTURE ---
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
MASTER_LOG_FILE = os.path.join(LOG_DIR, "master_sync.log")

logger = logging.getLogger("SyncManager")
logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(MASTER_LOG_FILE, encoding='utf-8')
fh.setLevel(logging.DEBUG)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)

formatter = logging.Formatter('%(asctime)s | [%(levelname)s] | %(message)s')
fh.setFormatter(formatter)
ch.setFormatter(formatter)

logger.addHandler(fh)
logger.addHandler(ch)

if sys.stdout is not None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

if sys.executable.lower().endswith("pythonw.exe") or sys.stdout is None:
    class BlackHole:
        def write(self, _): pass
        def flush(self): pass
        def isatty(self): return False
    sys.stdout = BlackHole()
    sys.stderr = BlackHole()

def clear_old_logs():
    logger.info("Step 1: Cleaning up old individual task logs...")
    files_removed = 0
    for task in SYNC_TASKS:
        log_file = os.path.join(LOG_DIR, f"{task['name'].replace(' ', '_')}.log")
        if os.path.exists(log_file):
            try:
                os.remove(log_file)
                files_removed += 1
            except Exception as e:
                logger.warning(f"Could not delete old log {log_file}: {e}")
    logger.info(f"Cleanup complete. Removed {files_removed} old log files.")

def parse_robocopy_log(log_path):
    stats = {"total": 0, "copied": 0, "skipped": 0, "failed": 0, "extras": 0}
    time.sleep(1)
    
    if not os.path.exists(log_path):
        logger.error(f"Stat Parsing Error: Log file not found at {log_path}")
        return stats
        
    try:
        with open(log_path, 'rb') as f:
            raw_data = f.read()
        
        content = ""
        for encoding in['utf-16', 'utf-8', 'cp1252']:
            try:
                content = raw_data.decode(encoding)
                if "ROBOCOPY" in content:
                    break
            except:
                continue

        pattern = r"Files\s*:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"
        matches = re.findall(pattern, content)

        if matches:
            final_stats = matches[-1]
            stats["total"]   = int(final_stats[0])
            stats["copied"]  = int(final_stats[1])
            stats["skipped"] = int(final_stats[2])
            stats["failed"]  = int(final_stats[4])
            stats["extras"]  = int(final_stats[5])
            logger.debug(f"Successfully parsed stats for {os.path.basename(log_path)}")
        else:
            logger.warning(f"Stat Parsing Warning: Regex could not find the Files summary in {log_path}")
                
    except Exception as e:
        logger.error(f"Stat Parsing Failed with exception: {e}")
        
    return stats

def ensure_nas_connection():
    # Attempt to pull destination paths to ping connections
    destinations = set()
    for task in SYNC_TASKS:
        # Extract the root share path (e.g. \\TRUENAS\storage)
        match = re.match(r"^(\\\\[^\\]+\\[^\\]+)", task['destination'])
        if match:
            destinations.add(match.group(1))
            
    if not destinations:
        return
        
    logger.info("Step 2: Pinging Network connections...")
    for nas_path in destinations:
        try:
            res = subprocess.run(
                f'net use "{nas_path}"', 
                shell=True, 
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if res.returncode == 0:
                logger.info(f"NAS Check: {nas_path} is alive and mapped.")
            else:
                logger.warning(f"NAS Check: {nas_path} might not be mapped yet. (Code: {res.returncode})")
        except Exception as e:
            logger.error(f"NAS Check: Critical error while checking connection {nas_path}: {e}")

def format_discord_block(res):
    icon = "✅" if (res['success'] and res['stats']['failed'] == 0) else "❌"
    return (
        f"**{icon} {res['name']}**\n"
        f"`{res['source']}` ➜ `{res['destination']}`\n"
        f"```ini\n"
        f"[Statistics]\n"
        f"Files Copied : {res['stats']['copied']}\n"
        f"Files Scanned: {res['stats']['total']}\n"
        f"Failed Items : {res['stats']['failed']}\n"
        f"Elapsed Time : {res['duration']:.2f}s\n"
        f"```"
    )

def send_summary_notification(results):
    logger.info("Step 4: Compiling final results...")
    
    if not WEBHOOK_URL:
        logger.info("DISCORD_WEBHOOK_URL is not set in .env. Skipping Discord notification.")
        return
        
    total_copied = sum(r['stats']['copied'] for r in results)
    total_failed = sum(r['stats']['failed'] for r in results)
    global_failure = any(r['stats']['failed'] > 0 or not r['success'] for r in results)
    
    color = 15548997 if global_failure else 5763719
    description_blocks = [format_discord_block(r) for r in results]
    
    payload = {
        "embeds":[{
            "title": f"{'⚠️' if global_failure else '✅'} Backup Run Complete ({len(results)} Tasks)",
            "description": "\n".join(description_blocks),
            "color": color,
            "footer": {
                "text": f"Global Stats: {total_copied} Copied • {total_failed} Failed"
            },
            "timestamp": datetime.utcnow().isoformat()
        }]
    }
    
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
        logger.info("Webhook successfully delivered.")
    except Exception as e:
        logger.error(f"Webhook delivery failed: {e}")

def run_sync():
    logger.info("==========================================")
    logger.info("        STARTING BACKUP ENGINE           ")
    logger.info("==========================================")
    
    clear_old_logs()
    ensure_nas_connection()
    
    results =[]
    
    for i, task in enumerate(SYNC_TASKS, 1):
        name = task['name']
        src = task['source']
        dest = task['destination']
        threads = task.get('threads', 8)
        
        # Determine Mode
        mode = task.get('mode', 'direct').lower()
        log_file = os.path.join(LOG_DIR, f"{name.replace(' ', '_')}.log")
        
        logger.info(f"--- Task {i}/{len(SYNC_TASKS)}: Starting[{name}] (Mode: {mode}) ---")
        
        if not os.path.exists(src):
            logger.error(f"Task Failed: Source path does not exist: {src}")
            results.append({
                "name": name, "source": src, "destination": dest, "success": False, "duration": 0,
                "stats": {"total":0, "copied":0, "failed":1}
            })
            continue

        # --- HISTORY MODE ---
        if mode in ['history', 'versions']:
            days_to_keep = task.get('days', 7)
            base_dest = dest
            today_str = datetime.now().strftime("%Y-%m-%d")
            
            # Change destination to point inside today's folder
            dest = os.path.join(base_dest, today_str)
            logger.info(f"Task [{name}]: Target adjusted to {dest}. Keeping {days_to_keep} days.")
            
            # Ensure base destination exists so we can scan/delete old folders
            if not os.path.exists(base_dest):
                try:
                    os.makedirs(base_dest, exist_ok=True)
                except Exception as e:
                    logger.error(f"Could not create base destination {base_dest}: {e}")
            
            if os.path.exists(base_dest):
                try:
                    existing_backups =[]
                    # Find folders matching YYYY-MM-DD
                    for folder in os.listdir(base_dest):
                        folder_path = os.path.join(base_dest, folder)
                        if os.path.isdir(folder_path) and re.match(r"^\d{4}-\d{2}-\d{2}$", folder):
                            existing_backups.append(folder)
                    
                    existing_backups.sort() # Oldest dates will be first
                    
                    # If today's backup isn't in the list, we need to account for it being created
                    target_count = days_to_keep if today_str in existing_backups else days_to_keep - 1
                    
                    # Delete oldest backups until we hit our limit
                    while len(existing_backups) > target_count and target_count >= 0:
                        oldest = existing_backups.pop(0)
                        oldest_path = os.path.join(base_dest, oldest)
                        logger.info(f"Task[{name}]: Deleting expired backup -> {oldest}")
                        shutil.rmtree(oldest_path)
                        
                except Exception as e:
                    logger.error(f"Task [{name}]: Failed during history cleanup: {e}")

        start_time = time.time()
        
        cmd =[
            "robocopy", src, dest, "/MIR", 
            "/COPY:DAT", "/DCOPY:DAT", "/FFT",
            "/R:3", "/W:5", f"/MT:{threads}", 
            "/NP", "/TS", "/FP", f"/LOG:{log_file}", 
            "/TEE", "/XJ"
        ]
        
        try:
            logger.debug(f"Command: {' '.join(cmd)}")
            process = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                shell=False, 
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            is_success = process.returncode < 8
            logger.info(f"Task Complete: {name} exited with code {process.returncode}")
            
            if not is_success:
                logger.error(f"Robocopy encountered errors during {name}")

        except Exception as e:
            logger.error(f"CRITICAL EXCEPTION during task {name}:")
            logger.error(traceback.format_exc())
            is_success = False

        duration = time.time() - start_time
        stats = parse_robocopy_log(log_file)
        
        results.append({
            "name": name,
            "source": src,
            "destination": dest,
            "success": is_success,
            "duration": duration,
            "stats": stats
        })
        
        logger.info(f"Heartbeat: Task [{name}] finished in {duration:.2f}s. Moving to next task...")

    logger.info("All tasks in SYNC_TASKS have been processed.")
    send_summary_notification(results)
    logger.info("==========================================")
    logger.info("        BACKUP ENGINE SHUTDOWN           ")
    logger.info("==========================================")


if __name__ == "__main__":
    try:
        run_sync()
    except Exception as e:
        logger.critical("THE ENTIRE SCRIPT CRASHED!")
        logger.critical(traceback.format_exc())