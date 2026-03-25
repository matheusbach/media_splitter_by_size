import subprocess
import time

def test_kf():
    subprocess.run([
        "ffmpeg", "-y", "-ss", "600", "-i", "live.mp4", "-t", "5",
        "-c", "copy", "test_605.mp4"
    ], capture_output=True)
    
    p2 = subprocess.run([
        "ffprobe", "-v", "error", "-read_intervals", "0%3",
        "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
        "-of", "csv=p=0", "test_605.mp4"
    ], capture_output=True, text=True)
    
    print("test_605 first packets:")
    lines = p2.stdout.strip().split("\n")
    print(lines[:5])

if __name__ == "__main__":
    test_kf()