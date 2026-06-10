import subprocess
import time
process = subprocess.Popen(["python", "-c", "import time, sys; [sys.stderr.write(f'line {i}\\n') or time.sleep(0.5) for i in range(5)]"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

start = time.time()
while True:
    try:
        out, err = process.communicate(timeout=1)
        break
    except subprocess.TimeoutExpired:
        print("Timeout! 1 second passed")

print("Finished!", err)
