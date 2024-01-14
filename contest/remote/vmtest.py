#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

import configparser
import datetime
import unicodedata
import subprocess
from time import sleep
import fcntl
import os
import psutil
import signal

from lib import Fetcher


"""
Config:

[executor]
name=executor
group=test-group
test=test-name
[remote]
branches=https://url-to-branches-manifest
[local]
base_path=/common/path
json_path=base-relative/path/to/json
results_path=base-relative/path/to/raw/outputs
tree_path=/root-path/to/kernel/git
[www]
url=https://url-to-reach-base-path
# Specific stuff
[env]
paths=/extra/exec/PATH:/another/bin
[vm]
paths=/extra/exec/PATH:/another/bin
init_prompt=expected_on-boot#
virtme_opt=--opt,--another one
default_timeout=15
boot_timeout=45


Expected:
group1 test1 skip
group1 test3 fail
group3 testV skip
"""


def decode_and_filter(buf):
    while True:
        ctrl_seq = buf.find(b'\x1b[?2004')
        if ctrl_seq == -1:
            break
        buf = buf[:ctrl_seq] + buf[ctrl_seq + 8:]

    buf = buf.decode("utf-8", "ignore")
    return "".join([x for x in buf if (x in ['\n'] or unicodedata.category(x)[0]!="C")])


class VM:
    def __init__(self, config):
        self.fail_state = ""
        self.p = None
        self.children = []
        self.config = config

        self.cfg_boot_to = int(config.get('vm', 'boot_timeout'))

        self.log_out = ""
        self.log_err = ""

    def virt_popen(self, cmd):
        env = os.environ.copy()
        if self.config.get('env', 'paths'):
            env['PATH'] += ':' + self.config.get('env', 'paths')

        return subprocess.Popen(cmd, env=env, cwd=self.config.get('local', 'tree_path'),
                                stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def build(self):
        if self.log_out or self.log_err:
            raise Exception("Logs were not flushed before calling build")

        print("INFO: building kernel")
        # Make sure we rebuild, vng seems to skip building if config is already there
        cfg_file = self.config.get('local', 'tree_path') + '/.config'
        if os.path.exists(cfg_file):
            print("INFO: removing old config")
            os.remove(cfg_file)

        proc = self.virt_popen("vng -v -b -f .nsim_config".split())
        stdout, stderr = proc.communicate()
        self.log_out = stdout.decode("utf-8", "ignore")
        self.log_err = stderr.decode("utf-8", "ignore")
        proc.stdout.close()
        proc.stderr.close()


    def start(self):
        cmd = "vng -v -r arch/x86/boot/bzImage --cwd tools/testing/selftests/drivers/net/netdevsim/ --user root"
        cmd = cmd.split(' ')
        cmd += self.config.get('vm', 'virtme_opt').split(',')

        print("INFO: VM starting:", " ".join(cmd))
        self.p = self.virt_popen(cmd)

        for pipe in [self.p.stdout, self.p.stderr]:
            flags = fcntl.fcntl(pipe, fcntl.F_GETFL)
            fcntl.fcntl(pipe, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # get the output
        init_prompt = self.config.get('vm', 'init_prompt')
        if init_prompt[-1] != ' ':
            init_prompt += ' '
        print(f"INFO: expecting prompt: '{init_prompt}'")
        self.drain_to_prompt(prompt=init_prompt, dump_after=self.cfg_boot_to)

        print("INFO: reached initial prompt")
        self.cmd("PS1='xx__-> '")
        self.drain_to_prompt()

        # Install extra PATHs
        if self.config.get('vm', 'paths'):
            self.cmd("export PATH=" + self.config.get('vm', 'paths') + ':$PATH')
            self.drain_to_prompt()

        # Save the children, we'll need to kill them on crash
        self.children = psutil.Process(self.p.pid).children(recursive=True)

    def stop(self):
        self.cmd("exit")
        try:
            stdout, stderr = self.p.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            print("WARNING: process did not exit, sending a KILL to", self.p.pid, self.children)
            for c in self.children:
                try:
                    c.kill()
                except psutil.NoSuchProcess:
                    pass
            stdout, stderr = self.p.communicate(timeout=2)

        self.p.stdout.close()
        self.p.stderr.close()
        stdout = stdout.decode("utf-8", "ignore")
        stderr = stderr.decode("utf-8", "ignore")

        print("INFO: VM stopped")
        self.log_out += stdout
        self.log_err += stderr


    def cmd(self, command):
        buf = command.encode('utf-8')
        if buf[-1] != '\n':
            buf += b'\n'
        self.p.stdin.write(buf)
        self.p.stdin.flush()

    def ctrl_c(self):
        self.log_out += '\nCtrl-C stdout\n'
        self.log_err += '\nCtrl-C stderr\n'
        self.p.stdin.write(b'\x03')
        self.p.stdin.flush()

    def _read_pipe_nonblock(self, pipe):
        read_some = False
        output = ""
        try:
            buf = os.read(pipe.fileno(), 1024)
            if not buf:
                return read_some, output
            read_some = True
            output = decode_and_filter(buf)
            if output.find("] RIP: ") != -1 or output.find("] Call Trace:") != -1:
                self.fail_state = "oops"
        except BlockingIOError:
            pass
        return read_some, output

    def drain_to_prompt(self, prompt="xx__-> ", dump_after=None):
        if dump_after is None:
            dump_after = int(self.config.get('vm', 'default_timeout'))
        waited = 0
        total_wait = 0
        stdout = ""
        stderr = ""
        while True:
            read_some, out = self._read_pipe_nonblock(self.p.stdout)
            self.log_out += out
            stdout += out
            read_som2, err = self._read_pipe_nonblock(self.p.stderr)
            read_some |= read_som2
            self.log_err += err
            stderr += err

            if read_some:
                if stdout.endswith(prompt):
                    break
                waited = 0
            else:
                total_wait += 0.03
                waited += 0.03
                sleep(0.03)

            if waited > dump_after:
                print("WAIT TIMEOUT retcode:", self.p.returncode)
                self.log_out += '\nWAIT TIMEOUT stdout\n'
                self.log_err += '\nWAIT TIMEOUT stderr\n'
                raise TimeoutError(stderr, stdout)

        return stdout, stderr

    def dump_log(self, dir_path, result=None, info=None):
        os.makedirs(dir_path)

        if self.log_out:
            with open(os.path.join(dir_path, 'stdout'), 'w') as fp:
                fp.write(self.log_out)
        if self.log_err:
            with open(os.path.join(dir_path, 'stderr'), 'w') as fp:
                fp.write(self.log_err)
        if result is not None:
            with open(os.path.join(dir_path, 'result'), 'w') as fp:
                fp.write(repr(result))
        if info is not None:
            strinfo = ""
            for k, v in info.items():
                strinfo += f'{k}:\t{v}\n'
            with open(os.path.join(dir_path, 'info'), 'w') as fp:
                fp.write(strinfo)

        self.log_out = ""
        self.log_err = ""

    def bash_prev_retcode(self):
        self.cmd("echo $?")
        stdout, stderr = self.drain_to_prompt()
        return int(stdout.split('\n')[1])


def test(binfo, rinfo, config):
    print("Run at", datetime.datetime.now())

    results_path = os.path.join(config.get('local', 'base_path'),
                                config.get('local', 'results_path'),
                                rinfo['run-cookie'])
    os.makedirs(results_path)

    link = config.get('www', 'url') + '/' + \
           config.get('local', 'results_path') + '/' + \
           rinfo['run-cookie']

    cases = []

    vm = VM(config)
    vm.build()
    vm.dump_log(results_path + '/build')

    vm.start()
    vm_id = 0
    vm.dump_log(results_path + '/vm-start-' + str(vm_id))

    dir_path = config.get('local', 'tree_path') + "/tools/testing/selftests/drivers/net/netdevsim"
    for test in os.listdir(dir_path):
        file_path = os.path.join(dir_path, test)
        if not os.path.isfile(file_path) or not os.access(file_path, os.X_OK):
            print("< skip " + test)
            continue

        print(f"INFO: running test ===", test)
        vm.cmd("./" + test)

        try:
            vm.drain_to_prompt()
            if vm.fail_state:
                retcode = 1
            else:
                retcode = vm.bash_prev_retcode()
        except TimeoutError:
            vm.ctrl_c()
            vm.drain_to_prompt()
            retcode = 1

        indicators = {
            "fail": vm.log_out.find("[FAIL]") != -1,
            "skip": vm.log_out.find("[SKIP]") != -1,
            "pass": vm.log_out.find("[OKAY]") != -1 or vm.log_out.find("[PASS]") != -1 or \
                    vm.log_out.find("[ OK ]") != -1 or vm.log_out.find("[OK]") != -1,
        }

        result = 'pass'
        if indicators["skip"] or not indicators["pass"]:
            result = 'skip'

        if retcode == 4:
            result = 'skip'
        elif retcode:
            result = 'fail'
        if indicators["fail"]:
            result = 'fail'

        vm.dump_log(results_path + '/' + test, result=retcode,
                    info={"vm-id": vm_id, "found": indicators})

        print("> retcode:", retcode, "result:", result, "found", indicators)

        cases.append({'test': test, 'group': 'netdevsim', 'result': result,
                      'link': link + '/' + test})

        if vm.fail_state:
            print("INFO: VM kernel crashed, starting a clean one!")
            vm.stop()
            vm.dump_log(results_path + '/vm-stop-' + str(vm_id))
            vm = VM(config)
            vm.start()
            vm_id += 1
            vm.dump_log(results_path + '/vm-start-' + str(vm_id))

    vm.stop()
    vm.dump_log(results_path + '/vm-stop-' + str(vm_id))

    print("Done at", datetime.datetime.now())

    return cases


def main() -> None:
    config = configparser.ConfigParser()
    config.read(['remote.config'])

    base_dir = config.get('local', 'base_path')

    f = Fetcher(test, config,
                name=config.get('executor', 'name'),
                branches_url=config.get('remote', 'branches'),
                results_path=os.path.join(base_dir, config.get('local', 'json_path')),
                url_path=config.get('www', 'url') + '/' + config.get('local', 'json_path'),
                tree_path=config.get('local', 'tree_path'))
    f.run()


if __name__ == "__main__":
    main()
