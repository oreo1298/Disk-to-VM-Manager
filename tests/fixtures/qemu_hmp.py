"""Monitor commands to the appliance VM's QEMU, for the appliance test (test_appliance.py).

Runs as root inside the emulated appliance:

    python3 qemu_hmp.py 'trace-event input_event_rel on' 'info mice'

It talks QMP on the VM's control socket (the appliance keeps it in /run/rehome) and
prints each command's reply.
"""

import glob
import json
import socket
import sys

path = glob.glob("/run/rehome/vms/*/qmp.sock")[0]
sock = socket.socket(socket.AF_UNIX)
sock.connect(path)
f = sock.makefile("rw")
f.readline()                                    # the greeting


def execute(cmd, args=None):
    f.write(json.dumps({"execute": cmd, **({"arguments": args} if args else {})}) + "\n")
    f.flush()
    while True:
        reply = json.loads(f.readline())
        if "event" not in reply:
            return reply


execute("qmp_capabilities")
for line in sys.argv[1:]:
    print(line, "->", execute("human-monitor-command", {"command-line": line}))
