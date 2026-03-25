import subprocess
out = subprocess.check_output('ffprobe -v error -select_streams v:0 -show_entries packet=pts_time,flags -of csv=p=0 live.mp4 | head -n 3000', shell=True, text=True)
kfs = [line.split(',')[0] for line in out.splitlines() if 'K' in line]
print(kfs)
