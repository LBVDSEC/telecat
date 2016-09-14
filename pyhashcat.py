#!/usr/bin/env python
import os
import re
import subprocess
import tempfile
import shlex
import sys
import time
import logging

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

INCLUDE_CRACKED = True
HASHCAT_PATH = '/usr/local/bin/hashcat'
STATUS_LINE_RE = '([A-Z_]+?)\s*([^A-Z_]+)\s*'

(STATUS_INITIALIZING, STATUS_STARTING, STATUS_RUNNING, STATUS_PAUSED,
 STATUS_EXHAUSTED, STATUS_CRACKED, STATUS_ABORTED, STATUS_QUIT,
 STATUS_BYPASS, STATUS_CHECKPOINT, STATUS_AUTOTUNING) = range(0, 11)

STATUS_CODES = {
    STATUS_INITIALIZING: "Initializing",
    STATUS_STARTING: "Starting",
    STATUS_RUNNING: "Running",
    STATUS_PAUSED: "Paused",
    STATUS_EXHAUSTED: "Exhausted",
    STATUS_CRACKED: "Cracked",
    STATUS_ABORTED: "Aborted",
    STATUS_QUIT: "Quit",
    STATUS_BYPASS: "Bypass",
    STATUS_CHECKPOINT: "Running (Stop at Checkpoint)",
    STATUS_AUTOTUNING: "Autotuning"
}

(EXIT_GPU_ALARM, EXIT_ERROR, EXIT_CRACKED, EXIT_EXHAUSTED, EXIT_ABORTED) = (254, 255, 0, 1, 2)
EXIT_CODES = {
    EXIT_GPU_ALARM: "GPU-Watchdog Alarm",  # -2
    EXIT_ERROR: "Error",  # -1
    EXIT_CRACKED: "OK/Cracked",
    EXIT_EXHAUSTED: "Exhausted",
    EXIT_ABORTED: "Aborted"
}


class HashcatController(object):

    def __init__(self, command_line, stop_event):
        self.stats = {}
        self.process = None
        self.outfile_name = None
        self._delete_outfile = False
        self._do_clean_up = True
        self.output = None
        self.stop_event = stop_event
        self.command_line = self.build_command_line(command_line)

    def build_command_line(self, command_line):
        required_args = [HASHCAT_PATH, '--quiet', '--status', '--machine-readable']
        if str is type(command_line):
            command_line = shlex.split(command_line)

        for arg in ['--quiet', '--status', '--machine-readable']:
            if arg in command_line:
                command_line.pop(command_line.index(arg))

        if "hashcat" in command_line[0]:
            command_line.pop(0)

        if "-o" in command_line:
            outfile_name = command_line[command_line.index("-o")+1]
        elif "--outfile" in command_line:
            outfile_name = command_line[command_line.index("--outfile")+1]
        else:
            self._delete_outfile = True
            outfile = tempfile.NamedTemporaryFile()
            command_line += ['-o', outfile.name]
            outfile_name = outfile.name
        self.outfile_name = outfile_name
        return required_args + command_line

    def launch(self):
        self.process = subprocess.Popen(self.command_line, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.PIPE)
        time.sleep(1)  # Give hashcat a chance to die fast on errors
        return self.process

    def run(self, stats_timer=3):
        self.stop_event.clear()
        self.launch()
        while self.process.poll() is None:
            stats = self.parse_status_line()
            if self.stop_event.wait(stats_timer):
                logger.debug("Got stop event, quiting")
                self.quit()
        self.clean_up()
        self.stop_event.set()

    def clean_up(self):
        if not self._do_clean_up:
            return

        output = None
        if EXIT_ERROR == self.process.returncode:
            output = self.get_output()
        elif EXIT_CRACKED == self.process.returncode:
            try:
                with open(self.outfile_name, 'r') as outfile:
                    _cracked = outfile.read()
                    outfile.close()
            except IOError:
                _cracked = ""
            if len(_cracked) == 0:
                output = "Found in POT file"
            elif INCLUDE_CRACKED:
                output = _cracked

        if self._delete_outfile:
            try:
                os.remove(self.outfile_name)
            except OSError as e:
                pass
        self.output = output

    def _get_line(self):
        if self.process and self.process.poll() is None:
            return self.process.stdout.readline()
        else:
            return ""

    def _send_line(self, line):
        if self.process and self.process.poll() is None:
            self.process.stdin.write(line)

    def wait_for_pattern(self, pattern):
        line = self._get_line()
        while self.process and self.process.poll() is None:
            if pattern in line:
                return line
            else:
                line = self._get_line()
        return ""

    def get_stats(self):
        self._send_line('s')
        return self.wait_for_pattern('STATUS')

    def pause(self):
        self._send_line('p')
        if 'Paused' in self.wait_for_pattern('Paused'):
            return True
        return False

    def resume(self):
        self._send_line('r')
        if 'Resumed' in self.wait_for_pattern('Resumed'):
            return True
        return False

    def quit(self):
        if self.process.poll() is None:
            self._send_line('q')
            self.parse_status_line(self.wait_for_pattern('STATUS'))
            self.clean_up()

    def parse_status_line(self, status_line=None):
        stats = {}
        if status_line is None:
            status_line = self.get_stats()

        if not status_line.startswith("STATUS"):
            return

        for key, value in re.findall(STATUS_LINE_RE, status_line):
            value = value.strip()
            if 'SPEED' == key:
                values = value.split()
                value = [(int(cnt), float(ms)) for (cnt, ms) in zip(values[::2], values[1::2])]
            elif 'EXEC_RUNTIME' == key:
                value = [float(k) for k in value.split()]
            elif 'TEMP' == key:
                value = [int(k) for k in value.split()]
            elif key in ['PROGRESS', 'RECHASH', 'RECSALT']:
                cur, left = value.split()
                value = (int(cur), int(left))
            else:
                if "." in value:
                    value = float(value)
                else:
                    value = int(value)
            stats[key] = value
        self.stats = stats
        return stats

    def get_output(self):
        (stdout, stderr) = self.process.communicate()
        return "\n".join(l for l in stdout.split('\n') + stderr.split('\n') if l)
