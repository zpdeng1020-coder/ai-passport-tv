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
