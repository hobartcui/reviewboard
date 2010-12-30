import os
import urlparse

from django.utils.translation import ugettext_lazy as _
import paramiko

from reviewboard.scmtools.errors import AuthenticationError, \
                                        BadHostKeyError, SCMError, \
                                        UnknownHostKeyError


# A list of known SSH URL schemes.
ssh_uri_schemes = ["ssh", "sftp"]

urlparse.uses_netloc.extend(ssh_uri_schemes)


class RaiseUnknownHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """A Paramiko policy that raises UnknownHostKeyError for missing keys."""
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKeyError(hostname, key)


def humanize_key(key):
    """Returns a human-readable key as a series of hex characters."""
    return ':'.join(["%02x" % ord(c) for c in key.get_fingerprint()])


def get_host_keys_filename():
    """Returns the URL to the known host keys file."""
    return os.path.expanduser('~/.ssh/known_hosts')


def get_user_key():
    """Returns the keypair of the user running Review Board.

    This will be an instance of :py:mod:`paramiko.PKey`, representing
    a DSS or RSA key, as long as one exists. Otherwise, it may return None.
    """
    keyfiles = []

    for cls, filename in ((paramiko.RSAKey, 'id_rsa'),
                          (paramiko.DSSKey, 'id_dsa')):
        # Paramiko looks in ~/.ssh and ~/ssh, depending on the platform,
        # so check both.
        for sshdir in ('.ssh', 'ssh'):
            path = os.path.expanduser('~/%s/%s' % (sshdir, filename))

            if os.path.isfile(path):
                keyfiles.append((cls, path))

    for cls, keyfile in keyfiles:
        try:
            return cls.from_private_key_file(keyfile)
        except paramiko.SSHException, e:
            logging.error('SSH: Unknown error accessing local key file %s: %s'
                          % (keyfile, e))
        except paramiko.PasswordRequiredException, e:
            logging.error('SSH: Unable to access password protected key file '
                          '%s: %s' % (keyfile, e))
        except IOError, e:
            logging.error('SSH: Error reading local key file %s: %s'
                          % (keyfile, e))

    return None


def generate_user_key():
    """Generates a new RSA keypair for the user running Review Board.

    This will store the new key in $HOME/.ssh/id_rsa and return the
    resulting key as an instance of :py:mod:`paramiko.RSAKey`.

    If a key already exists in the id_rsa file, it's returned instead.

    Callers are expected to handle any exceptions. This may raise
    IOError for any problems in writing the key file, or
    paramiko.SSHException for any other problems.
    """
    filename = os.path.expanduser('~/.ssh/id_rsa')

    if os.path.isfile(filename):
        return get_user_key()

    parent_dir = os.path.dirname(filename)

    if not os.path.exists(parent_dir):
        os.mkdir(parent_dir, 0700)

    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(filename)
    return key


def is_ssh_uri(url):
    """Returns whether or not a URL represents an SSH connection."""
    return urlparse.urlparse(url)[0] in ssh_uri_schemes


def get_ssh_client():
    """Returns a new paramiko.SSHClient with all known host keys added."""
    client = paramiko.SSHClient()
    filename = get_host_keys_filename()

    if os.path.exists(filename):
        client.load_host_keys(filename)

    return client


def add_host_key(hostname, key):
    """Adds a host key to the known hosts file."""
    dirname = os.path.dirname(get_host_keys_filename())

    if not os.path.exists(dirname):
        # Make sure the .ssh directory exists.
        try:
            os.mkdir(dirname, 0700)
        except OSError, e:
            raise IOError(_("Unable to create directory %(dirname)s, which is "
                            "needed for the SSH host keys. Create this "
                            "directory, set the web server's user as the "
                            "the owner, and make it writable only by that "
                            "user.") % {
                'dirname': dirname,
            })

    filename = get_host_keys_filename()

    try:
        fp = open(filename, 'a')
        fp.write('%s %s %s\n' % (hostname, key.get_name(), key.get_base64()))
        fp.close()
    except IOError, e:
        raise IOError(
            _('Unable to write host keys file %(filename)s: %(error)s') % {
                'filename': filename,
                'error': e,
            })


def replace_host_key(hostname, old_key, new_key):
    """
    Replaces a host key in the known hosts file with another.

    This is used for replacing host keys that have changed.
    """
    filename = get_host_keys_filename()

    if not os.path.exists(filename):
        add_host_key(hostname, new_key)
        return

    try:
        fp = open(filename, 'r')
        lines = fp.readlines()
        fp.close()

        old_key_base64 = old_key.get_base64()
    except IOError, e:
        raise IOError(
            _('Unable to read host keys file %(filename)s: %(error)s') % {
                'filename': filename,
                'error': e,
            })

    try:
        fp = open(filename, 'w')

        for line in lines:
            parts = line.strip().split(" ")

            if parts[-1] == old_key_base64:
                parts[-1] = new_key.get_base64()

            fp.write(' '.join(parts) + '\n')

        fp.close()
    except IOError, e:
        raise IOError(
            _('Unable to write host keys file %(filename)s: %(error)s') % {
                'filename': filename,
                'error': e,
            })


def check_host(hostname, username=None, password=None):
    """
    Checks if we can connect to a host with a known key.

    This will raise an exception if we cannot connect to the host. The
    exception will be one of BadHostKeyError, UnknownHostKeyError, or
    SCMError.
    """
    client = get_ssh_client()
    client.set_missing_host_key_policy(RaiseUnknownHostKeyPolicy())

    try:
        client.connect(hostname, username=username, password=password)
    except paramiko.BadHostKeyException, e:
        raise BadHostKeyError(e.hostname, e.key, e.expected_key)
    except paramiko.AuthenticationException, e:
        # Some AuthenticationException instances have allowed_types set,
        # and some don't.
        allowed_types = getattr(e, 'allowed_types', [])

        if 'publickey' in allowed_types:
            key = get_user_key()
        else:
            key = None

        raise AuthenticationError(allowed_types, key)
    except paramiko.SSHException, e:
        raise SCMError(unicode(e))
