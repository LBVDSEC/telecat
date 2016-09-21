#!/usr/bin/env python
import os
import re
import subprocess
import tempfile
import shlex
import sys
import time
import logging
import threading

# TODO(gerry): Add a hash not found message when applicable

logging.basicConfig(level=logging.DEBUG,
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


STATE_NEW, STATE_STARTING, STATE_RUNNING, STATE_DONE = range(0, 4)
CONTROLLER_STATES = {
    STATE_NEW: 'New',
    STATE_STARTING: 'Starting new session',
    STATE_RUNNING: 'Running',
    STATE_DONE: 'Session complete'
}

class HashcatController(object):
    STATE_NEW, STATE_STARTING, STATE_RUNNING, STATE_DONE = range(0, 4)

    def __init__(self, stop_event=None):
        self.stats = None
        self.process = None
        self.outfile_name = None
        self._delete_outfile = False
        self._do_clean_up = True
        self.output = None
        self.error_output = None
        self.stop_event = stop_event is None and threading.Event() or stop_event
        self.state = self.STATE_NEW
        self._status_timer = 1
        self.p_event = threading.Event() # For pausing
        self.run_event = threading.Event() # To indicate session is running

    def build_command_line(self, command_line):
        required_args = [HASHCAT_PATH, '--quiet', '--status', '--machine-readable']
        if self._status_timer:
            required_args.append('--status-timer=%d' % self._status_timer)

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

    def status_monitor(self):
        self.output = []
        self.error_output = None
        self._status_line = None
        while self.process.poll() is None:
            if not self._status_timer:
                # will probably need to wrap this in try/except
                self.process.stdin.write('s')
            # will probably need to wrap this in try/except
            line = self.process.stdout.readline()
            if "Paused" in line:
                self.p_event.set()
            elif "Resumed" in line:
                self.p_event.clear()
            elif line.startswith("STATUS"):
                if not self.run_event.is_set():
                    self.run_event.set()
                stats = self.parse_status_line(line)  # or read?
                if stats:                
                    self.stats = stats

            if self.stop_event.is_set():
                logger.debug("Got stop event, quiting")
                self.quit()
                break
        self.state = self.STATE_DONE
        if EXIT_ERROR == self.process.returncode:
            stdout, stderr = self.process.communicate()
            self.output = "\n".join(l for l in stdout.split('\n') if l)
            self.error_output = "\n".join(l for l in stderr.split('\n') if l)
            logger.debug('Error: %s' % self.error_output)
        elif EXIT_CRACKED == self.process.returncode:
            if self.stats is None or self.stats['STATUS'] != STATUS_CRACKED:
                logger.debug(self.stats)
                logger.debug("Found hash in POT file")
            else:
                logger.debug("Session completed, Cracked")
                with open(self.outfile_name, 'r') as outfile:
                    self.cracked = outfile.read()
                    outfile.close()
        # process exited, update stuff
        if self._do_clean_up:
            self.clean_up()
        self.stop_event.set()

    def run(self, command_line, stats_timer=1):
        self.stop_event.clear()
        self.command_line = self.build_command_line(command_line)
        self.state = self.STATE_STARTING
        self.process = subprocess.Popen(self.command_line, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.PIPE)
        self.state = self.STATE_STARTING
        t = threading.Thread(target=self.status_monitor)    
        t.daemon = True
        t.start()
        while not self.stop_event.is_set():
            time.sleep(1)
        t.join()
        logger.debug("Done.")

    def clean_up(self):
        if self._delete_outfile:
            try:
                os.remove(self.outfile_name)
            except OSError as e:
                pass

    def is_running(self):
        return self.run_event.is_set()

    def is_paused(self):
        return self.p_event.is_set()

    def _get_line(self):
        if self.is_running():
            line = self.process.stdout.readline()
        else:
            return ""

    def _send_line(self, line):
        if self.is_running():
            self.process.stdin.write(line)

    def get_stats(self):
        return self.stats

    def pause(self):
        if not self.is_running() or self.p_event.is_set():
            return False
        logger.debug('Pausing...')
        self._send_line('p')
        # Wait for status monitor to confirm paused
        return self.p_event.wait(2)

    def resume(self):
        logger.debug('resuming...')
        self._send_line('r')
        # Wait for status_monitor to get the resumed line
        while self.p_event.is_set() and self.is_running():
            time.sleep(.5)
        return True

    def quit(self):
        if self.is_running:
            self._send_line('q')

    def parse_status_line(self, status_line):
        stats = {}
        if not status_line.startswith("STATUS"):
            return None
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
        return stats
