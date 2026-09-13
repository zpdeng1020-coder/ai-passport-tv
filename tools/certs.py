"""Finding the certificate authorities, which a downloaded build cannot assume.

HTTPS needs a list of certificate authorities to check the server's certificate
against. Python does not carry that list; it reads one from the machine it is
running on, at a path fixed when Python was compiled. That is correct for a
Python installed on the machine it runs on, and wrong for a program that was
built on one computer and downloaded to another.

This is not a hypothetical. The macOS build produced by CI could not load the
playlist at the default source, answering

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate (_ssl.c:1006)

while `curl` fetched the same address without complaint, and the same code run
from a checkout worked. The reason is the one above: the build machine's Python
records its own certificate path, and that path does not exist on the reader's
computer. Nothing was wrong with the address, the network, or the certificate.

The fix is to look for a bundle that is actually present, and to check that it
works rather than that it exists. An empty file, a directory that has moved, a
path that is right on one distribution and absent on the next -- all of them
produce a file that is there and a connection that still fails. So the test is
how many authorities ended up loaded, which is the only fact that matters and
the only one that cannot be faked by the file existing.

The operating system's own file is preferred to anything this program could
carry, and it is worth saying why, since carrying one would also work. A copy
inside the program is a snapshot of which authorities were trusted on the day
it was built, and it stays that snapshot for as long as nobody rebuilds. A
certificate authority that is later distrusted would go on being accepted. The
system's file is updated by the system, on the schedule security updates run.

The order is therefore: whatever the environment already says, then the path
Python would have used anyway, then the places each system keeps its bundle.
If none of them works, nothing is changed and the failure is left to speak for
itself -- silently trusting a certificate is not a way to make a connection
succeed.

Standard library only, like the rest of the server.
"""

from __future__ import annotations

import os
import ssl

# Where each system keeps the bundle its own tools use. Only the first existing
# one that actually loads authorities is taken, so the order is a preference
# between equivalents rather than a decision that has to be right.
#
# macOS has a single documented path and ships the file. The Linux entries cover
# the Debian family, the Fedora family and openSUSE, which between them account
# for the desktop distributions someone would run this on. Windows is absent on
# purpose: Python there loads the system certificate store through a different
# mechanism, `load_default_certs` already reaches it, and there is no file path
# to point at.
SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",                   # macOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",    # Fedora, RHEL, CentOS
    "/etc/ssl/ca-bundle.pem",              # openSUSE
)

# The variables OpenSSL consults, and therefore the ones that decide where the
# authorities come from. Named here so that a test can clear them before asking
# what this machine looks like without them: on a machine where they are set,
# every case below would otherwise be measuring the setting rather than the
# fallback.
ENVIRONMENT_VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR")

# The bundle this program adopted, if it had to adopt one; None when the machine
# was already in order. Kept because the two cases are indistinguishable
# afterwards -- the environment looks the same either way -- and a build check
# needs to tell them apart to know whether a release is safe to publish.
ADOPTED: str | None = None


def loaded_authorities() -> int:
    """How many certificate authorities a new default context would trust.

    Zero means every HTTPS connection will fail, whatever the reason given for
    any particular one. Asked of a real context rather than of the filesystem,
    because the failure being prevented here was a path that looked right.
    """
    try:
        context = ssl.create_default_context()
    except Exception:
        # A broken OpenSSL build, or a platform with no SSL at all. Reported as
        # "no authorities" because that is what it amounts to for the caller.
        return 0
    try:
        return int(context.cert_store_stats().get("x509_ca", 0))
    except Exception:
        # Very old Python, or a context that cannot report. Treated as unknown,
        # and unknown is not a reason to start rewriting the environment.
        return -1


def use_system_ca() -> str | None:
    """Make HTTPS verification work, using a bundle that is on this machine.

    Returns the path that was adopted, or None when nothing had to be done or
    nothing could be. Both are ordinary outcomes and neither is an error: a
    machine whose certificates already load needs no help, and a machine with no
    bundle at all cannot be helped from here.

    Safe to call more than once, and safe to call from every entry point, so no
    caller has to know whether another has already run.
    """
    if loaded_authorities() > 0:
        # The ordinary case, and the one that must stay a no-op: a Python
        # running where it was installed finds its bundle and nothing changes.
        # It is also the answer to whether a setting in the environment is worth
        # respecting -- a company bundle that loads is someone's deliberate
        # arrangement, and a path left over from a machine this program was
        # built on is not.
        return None

    # Whatever was in the environment is put back if none of the candidates
    # works. It describes a setup that does not verify, and putting it back is
    # still right: a variable this program overwrote and abandoned would be a
    # second unexplained failure after the first one.
    global ADOPTED
    previous = os.environ.get("SSL_CERT_FILE")
    for candidate in SYSTEM_CA_FILES:
        if not os.path.isfile(candidate):
            continue
        # Set, then check what was actually loaded. Testing the file first and
        # trusting the answer is what produced the original bug.
        os.environ["SSL_CERT_FILE"] = candidate
        if loaded_authorities() > 0:
            ADOPTED = candidate
            return candidate

    if previous is None:
        os.environ.pop("SSL_CERT_FILE", None)
    else:
        os.environ["SSL_CERT_FILE"] = previous
    return None
