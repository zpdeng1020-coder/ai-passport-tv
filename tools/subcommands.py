"""The names a bundled executable uses to become one of its parts.

In a module of its own so that both sides can agree without importing each
other. The launcher needs the names to start a part, and the entry point needs
them to recognise one -- and the entry point imports the launcher, so the launcher
importing the entry point back would be a cycle. It happened to work in one
import order and would fail in the other, which is not a property worth relying
on.

They are spelled to look internal on purpose. They are a protocol between two
processes of the same program, not a command-line interface: someone who types
one by accident should get the ordinary behaviour, not a server with no terminal
attached. Nothing here is documented for users.
"""

MEDIA_COMMAND = "__media"
CONFIG_COMMAND = "__config"

# Asks the program to report what it knows about its own certificate
# authorities, and exits. Not a part of the program -- a question put to it, by
# the build check that watches for the one failure a built executable cannot
# show from its source: a certificate path recorded on the machine it was built
# on and absent on the machine it was downloaded to. The build machine's own
# Python answers that question correctly, so it has to be the built program that
# is asked. See tools/smoke_test_package.py and tools/certs.py.
CERTS_COMMAND = "__certs"
