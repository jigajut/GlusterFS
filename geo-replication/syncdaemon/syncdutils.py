#
# Copyright (c) 2011-2014 Red Hat, Inc. <http://www.redhat.com>
# This file is part of GlusterFS.

# This file is licensed to you under your choice of the GNU Lesser
# General Public License, version 3 or any later version (LGPLv3 or
# later), or the GNU General Public License, version 2 (GPLv2), in all
# cases as published by the Free Software Foundation.
#

import os
import sys
import pwd
import time
import fcntl
import shutil
import logging
import socket
import errno
import threading
import subprocess
from subprocess import PIPE
from threading import Lock, Thread as baseThread
from errno import EACCES, EAGAIN, EPIPE, ENOTCONN, ECONNABORTED
from errno import EINTR, ENOENT, EPERM, ESTALE, EBUSY, errorcode
from signal import signal, SIGTERM
import select as oselect
from os import waitpid as owaitpid
import xml.etree.ElementTree as XET
from select import error as SelectError

from conf import GLUSTERFS_LIBEXECDIR, UUID_FILE
sys.path.insert(1, GLUSTERFS_LIBEXECDIR)
EVENTS_ENABLED = True
try:
    from events.eventtypes import GEOREP_FAULTY as EVENT_GEOREP_FAULTY
    from events.eventtypes import GEOREP_ACTIVE as EVENT_GEOREP_ACTIVE
    from events.eventtypes import GEOREP_PASSIVE as EVENT_GEOREP_PASSIVE
    from events.eventtypes import GEOREP_CHECKPOINT_COMPLETED \
        as EVENT_GEOREP_CHECKPOINT_COMPLETED
except ImportError:
    # Events APIs not installed, dummy eventtypes with None
    EVENTS_ENABLED = False
    EVENT_GEOREP_FAULTY = None
    EVENT_GEOREP_ACTIVE = None
    EVENT_GEOREP_PASSIVE = None
    EVENT_GEOREP_CHECKPOINT_COMPLETED = None

try:
    from cPickle import PickleError
except ImportError:
    # py 3
    from pickle import PickleError

from gconf import gconf

try:
    # py 3
    from urllib import parse as urllib
except ImportError:
    import urllib

try:
    from hashlib import md5 as md5
except ImportError:
    # py 2.4
    from md5 import new as md5

# auxiliary gfid based access prefix
_CL_AUX_GFID_PFX = ".gfid/"
GF_OP_RETRIES = 10

GX_GFID_CANONICAL_LEN = 37  # canonical gfid len + '\0'

CHANGELOG_AGENT_SERVER_VERSION = 1.0
CHANGELOG_AGENT_CLIENT_VERSION = 1.0
NodeID = None
rsync_version = None
SPACE_ESCAPE_CHAR = "%20"
NEWLINE_ESCAPE_CHAR = "%0A"
PERCENTAGE_ESCAPE_CHAR = "%25"


def sup(x, *a, **kw):
    """a rubyesque "super" for python ;)

    invoke caller method in parent class with given args.
    """
    return getattr(super(type(x), x),
                   sys._getframe(1).f_code.co_name)(*a, **kw)


def escape(s):
    """the chosen flavor of string escaping, used all over
       to turn whatever data to creatable representation"""
    return urllib.quote_plus(s)


def unescape(s):
    """inverse of .escape"""
    return urllib.unquote_plus(s)


def escape_space_newline(s):
    return s.replace("%", PERCENTAGE_ESCAPE_CHAR)\
            .replace(" ", SPACE_ESCAPE_CHAR)\
            .replace("\n", NEWLINE_ESCAPE_CHAR)


def unescape_space_newline(s):
    return s.replace(SPACE_ESCAPE_CHAR, " ")\
            .replace(NEWLINE_ESCAPE_CHAR, "\n")\
            .replace(PERCENTAGE_ESCAPE_CHAR, "%")


def norm(s):
    if s:
        return s.replace('-', '_')


def update_file(path, updater, merger=lambda f: True):
    """update a file in a transaction-like manner"""

    fr = fw = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
        try:
            fr = os.fdopen(fd, 'r+b')
        except:
            os.close(fd)
            raise
        fcntl.lockf(fr, fcntl.LOCK_EX)
        if not merger(fr):
            return

        tmpp = path + '.tmp.' + str(os.getpid())
        fd = os.open(tmpp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            fw = os.fdopen(fd, 'wb', 0)
        except:
            os.close(fd)
            raise
        updater(fw)
        os.fsync(fd)
        os.rename(tmpp, path)
    finally:
        for fx in (fr, fw):
            if fx:
                fx.close()


def create_manifest(fname, content):
    """
    Create manifest file for SSH Control Path
    """
    fd = None
    try:
        fd = os.open(fname, os.O_CREAT | os.O_RDWR)
        try:
            os.write(fd, content)
        except:
            os.close(fd)
            raise
    finally:
        if fd is not None:
            os.close(fd)


def setup_ssh_ctl(ctld, remote_addr, resource_url):
    """
    Setup GConf ssh control path parameters
    """
    gconf.ssh_ctl_dir = ctld
    content = "SLAVE_HOST=%s\nSLAVE_RESOURCE_URL=%s" % (remote_addr,
                                                        resource_url)
    content_md5 = md5hex(content)
    fname = os.path.join(gconf.ssh_ctl_dir,
                         "%s.mft" % content_md5)

    create_manifest(fname, content)
    ssh_ctl_path = os.path.join(gconf.ssh_ctl_dir,
                                "%s.sock" % content_md5)
    gconf.ssh_ctl_args = ["-oControlMain=auto", "-S", ssh_ctl_path]


def grabfile(fname, content=None):
    """open @fname + contest for its fcntl lock

    @content: if given, set the file content to it
    """
    # damn those messy open() mode codes
    fd = os.open(fname, os.O_CREAT | os.O_RDWR)
    f = os.fdopen(fd, 'r+b', 0)
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except:
        ex = sys.exc_info()[1]
        f.close()
        if isinstance(ex, IOError) and ex.errno in (EACCES, EAGAIN):
            # cannot grab, it's taken
            return
        raise
    if content:
        try:
            f.truncate()
            f.write(content)
        except:
            f.close()
            raise
    gconf.permanent_handles.append(f)
    return f


def grabpidfile(fname=None, setpid=True):
    """.grabfile customization for pid files"""
    if not fname:
        fname = gconf.pid_file
    content = None
    if setpid:
        content = str(os.getpid()) + '\n'
    return grabfile(fname, content=content)

final_lock = Lock()

mntpt_list = []
def finalize(*a, **kw):
    """all those messy final steps we go trough upon termination

    Do away with pidfile, ssh control dir and logging.
    """

    final_lock.acquire()
    if getattr(gconf, 'pid_file', None):
        rm_pidf = gconf.pid_file_owned
        if gconf.cpid:
            # exit path from parent branch of daemonization
            rm_pidf = False
            while True:
                f = grabpidfile(setpid=False)
                if not f:
                    # child has already taken over pidfile
                    break
                if os.waitpid(gconf.cpid, os.WNOHANG)[0] == gconf.cpid:
                    # child has terminated
                    rm_pidf = True
                    break
                time.sleep(0.1)
        if rm_pidf:
            try:
                os.unlink(gconf.pid_file)
            except:
                ex = sys.exc_info()[1]
                if ex.errno == ENOENT:
                    pass
                else:
                    raise
    if gconf.ssh_ctl_dir and not gconf.cpid:
        def handle_rm_error(func, path, exc_info):
            if exc_info[1].errno == ENOENT:
                return
            raise exc_info[1]

        shutil.rmtree(gconf.ssh_ctl_dir, onerror=handle_rm_error)
    if getattr(gconf, 'state_socket', None):
        try:
            os.unlink(gconf.state_socket)
        except:
            if sys.exc_info()[0] == OSError:
                pass

    """ Unmount if not done """
    for mnt in mntpt_list:
        p0 = subprocess.Popen (["umount", "-l", mnt], stderr=subprocess.PIPE)
        _, errdata = p0.communicate()
        if p0.returncode == 0:
            try:
                os.rmdir(mnt)
            except OSError:
                pass
        else:
            pass

    if gconf.log_exit:
        logging.info("exiting.")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(kw.get('exval', 0))



def log_raise_exception(excont):
    """top-level exception handler

    Try to some fancy things to cover up we face with an error.
    Translate some weird sounding but well understood exceptions
    into human-friendly lingo
    """
    is_filelog = False
    for h in logging.getLogger().handlers:
        fno = getattr(getattr(h, 'stream', None), 'fileno', None)
        if fno and not os.isatty(fno()):
            is_filelog = True

    exc = sys.exc_info()[1]
    if isinstance(exc, SystemExit):
        excont.exval = exc.code or 0
        raise
    else:
        logtag = None
        if isinstance(exc, GsyncdError):
            if is_filelog:
                logging.error(exc.args[0])
            sys.stderr.write('failure: ' + exc.args[0] + '\n')
        elif isinstance(exc, PickleError) or isinstance(exc, EOFError) or \
            ((isinstance(exc, OSError) or isinstance(exc, IOError)) and
             exc.errno == EPIPE):
            logging.error('connection to peer is broken')
            if hasattr(gconf, 'transport'):
                gconf.transport.wait()
                if gconf.transport.returncode == 127:
                    logging.error("getting \"No such file or directory\""
                                  "errors is most likely due to "
                                  "MISCONFIGURATION, please remove all "
                                  "the public keys added by geo-replication "
                                  "from authorized_keys file in subordinate nodes "
                                  "and run Geo-replication create "
                                  "command again.")
                    logging.error("If `gsec_create container` was used, then "
                                  "run `gluster volume geo-replication "
                                  "<MASTERVOL> [<SLAVEUSER>@]<SLAVEHOST>::"
                                  "<SLAVEVOL> config remote-gsyncd "
                                  "<GSYNCD_PATH> (Example GSYNCD_PATH: "
                                  "`/usr/libexec/glusterfs/gsyncd`)")
                gconf.transport.terminate_geterr()
        elif isinstance(exc, OSError) and exc.errno in (ENOTCONN,
                                                        ECONNABORTED):
            logging.error(lf('glusterfs session went down',
                             error=errorcode[exc.errno]))
        else:
            logtag = "FAIL"
        if not logtag and logging.getLogger().isEnabledFor(logging.DEBUG):
            logtag = "FULL EXCEPTION TRACE"
        if logtag:
            logging.exception(logtag + ": ")
            sys.stderr.write("failed with %s.\n" % type(exc).__name__)
        excont.exval = 1
        sys.exit(excont.exval)


class FreeObject(object):

    """wildcard class for which any attribute can be set"""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class Thread(baseThread):

    """thread class flavor for gsyncd

    - always a daemon thread
    - force exit for whole program if thread
      function coughs up an exception
    """

    def __init__(self, *a, **kw):
        tf = kw.get('target')
        if tf:
            def twrap(*aa):
                excont = FreeObject(exval=0)
                try:
                    tf(*aa)
                except:
                    try:
                        log_raise_exception(excont)
                    finally:
                        finalize(exval=excont.exval)
            kw['target'] = twrap
        baseThread.__init__(self, *a, **kw)
        self.setDaemon(True)


class GsyncdError(Exception):
    pass


class _MetaXattr(object):

    """singleton class, a lazy wrapper around the
    libcxattr module

    libcxattr (a heavy import due to ctypes) is
    loaded only when when the single
    instance is tried to be used.

    This reduces runtime for those invocations
    which do not need filesystem manipulation
    (eg. for config, url parsing)
    """

    def __getattr__(self, meth):
        from libcxattr import Xattr as LXattr
        xmeth = [m for m in dir(LXattr) if m[0] != '_']
        if meth not in xmeth:
            return
        for m in xmeth:
            setattr(self, m, getattr(LXattr, m))
        return getattr(self, meth)


Xattr = _MetaXattr()


def getusername(uid=None):
    if uid is None:
        uid = os.geteuid()
    return pwd.getpwuid(uid).pw_name


def privileged():
    return os.geteuid() == 0


def boolify(s):
    """
    Generic string to boolean converter

    return
    - Quick return if string 's' is of type bool
    - True if it's in true_list
    - False if it's in false_list
    - Warn if it's not present in either and return False
    """
    true_list = ['true', 'yes', '1', 'on']
    false_list = ['false', 'no', '0', 'off']

    if isinstance(s, bool):
        return s

    rv = False
    lstr = s.lower()
    if lstr in true_list:
        rv = True
    elif not lstr in false_list:
        logging.warn(lf("Unknown string in \"string to boolean\" conversion, "
                        "defaulting to False",
                        str=s))

    return rv


def eintr_wrap(func, exc, *a):
    """
    wrapper around syscalls resilient to interrupt caused
    by signals
    """
    while True:
        try:
            return func(*a)
        except exc:
            ex = sys.exc_info()[1]
            if not ex.args[0] == EINTR:
                raise


def select(*a):
    return eintr_wrap(oselect.select, oselect.error, *a)


def waitpid(*a):
    return eintr_wrap(owaitpid, OSError, *a)


def set_term_handler(hook=lambda *a: finalize(*a, **{'exval': 1})):
    signal(SIGTERM, hook)


def get_node_uuid():
    global NodeID
    if NodeID is not None:
        return NodeID

    NodeID = ""
    with open(UUID_FILE) as f:
        for line in f:
            if line.startswith("UUID="):
                NodeID = line.strip().split("=")[-1]
                break

    if NodeID == "":
        raise GsyncdError("Failed to get Host UUID from %s" % UUID_FILE)
    return NodeID


def is_host_local(host_id):
    return host_id == get_node_uuid()


def funcode(f):
    fc = getattr(f, 'func_code', None)
    if not fc:
        # python 3
        fc = f.__code__
    return fc


def memoize(f):
    fc = funcode(f)
    fn = fc.co_name

    def ff(self, *a, **kw):
        rv = getattr(self, '_' + fn, None)
        if rv is None:
            rv = f(self, *a, **kw)
            setattr(self, '_' + fn, rv)
        return rv
    return ff


def umask():
    return os.umask(0)


def entry2pb(e):
    return e.rsplit('/', 1)


def gauxpfx():
    return _CL_AUX_GFID_PFX


def md5hex(s):
    return md5(s).hexdigest()


def selfkill(sig=SIGTERM):
    os.kill(os.getpid(), sig)


def errno_wrap(call, arg=[], errnos=[], retry_errnos=[]):
    """ wrapper around calls resilient to errnos.
    """
    nr_tries = 0
    while True:
        try:
            return call(*arg)
        except OSError:
            ex = sys.exc_info()[1]
            if ex.errno in errnos:
                return ex.errno
            if not ex.errno in retry_errnos:
                raise
            nr_tries += 1
            if nr_tries == GF_OP_RETRIES:
                # probably a screwed state, cannot do much...
                logging.warn(lf('reached maximum retries',
                                args=repr(arg),
                                error=ex))
                raise
            time.sleep(0.250)  # retry the call


def lstat(e):
    return errno_wrap(os.lstat, [e], [ENOENT], [ESTALE, EBUSY])


def get_gfid_from_mnt(gfidpath):
    return errno_wrap(Xattr.lgetxattr,
                      [gfidpath, 'glusterfs.gfid.string',
                       GX_GFID_CANONICAL_LEN], [ENOENT], [ESTALE])


def matching_disk_gfid(gfid, entry):
    disk_gfid = get_gfid_from_mnt(entry)
    if isinstance(disk_gfid, int):
        return False

    if not gfid == disk_gfid:
        return False

    return True


class NoStimeAvailable(Exception):
    pass


class PartialHistoryAvailable(Exception):
    pass


class ChangelogHistoryNotAvailable(Exception):
    pass


class ChangelogException(OSError):
    pass


def gf_event(event_type, **kwargs):
    if EVENTS_ENABLED:
        from events.gf_event import gf_event as gfevent
        gfevent(event_type, **kwargs)


class GlusterLogLevel(object):
        NONE = 0
        EMERG = 1
        ALERT = 2
        CRITICAL = 3
        ERROR = 4
        WARNING = 5
        NOTICE = 6
        INFO = 7
        DEBUG = 8
        TRACE = 9


def get_changelog_log_level(lvl):
    return getattr(GlusterLogLevel, lvl, GlusterLogLevel.INFO)


def get_main_and_subordinate_data_from_args(args):
    main_name = None
    subordinate_data = None
    for arg in args:
        if arg.startswith(":"):
            main_name = arg.replace(":", "")
        if "::" in arg:
            subordinate_data = arg.replace("ssh://", "")

    return (main_name, subordinate_data)


def get_rsync_version(rsync_cmd):
    global rsync_version
    if rsync_version is not None:
        return rsync_version

    rsync_version = "0"
    p = subprocess.Popen([rsync_cmd, "--version"],
                         stderr=subprocess.PIPE,
                         stdout=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode == 0:
        rsync_version = out.split(" ", 4)[3]

    return rsync_version


def lf(event, **kwargs):
    """
    Log Format helper function, log messages can be
    easily modified to structured log format.
    lf("Config Change", sync_jobs=4, brick=/bricks/b1) will be
    converted as "Config Change<TAB>brick=/bricks/b1<TAB>sync_jobs=4"
    """
    msg = event
    for k, v in kwargs.items():
        msg += "\t{0}={1}".format(k, v)
    return msg


class Popen(subprocess.Popen):

    """customized subclass of subprocess.Popen with a ring
    buffer for children error output"""

    @classmethod
    def init_errhandler(cls):
        """start the thread which handles children's error output"""
        cls.errstore = {}

        def tailer():
            while True:
                errstore = cls.errstore.copy()
                try:
                    poe, _, _ = select(
                        [po.stderr for po in errstore], [], [], 1)
                except (ValueError, SelectError):
                    # stderr is already closed wait for some time before
                    # checking next error
                    time.sleep(0.5)
                    continue
                for po in errstore:
                    if po.stderr not in poe:
                        continue
                    po.lock.acquire()
                    try:
                        if po.on_death_row:
                            continue
                        la = errstore[po]
                        try:
                            fd = po.stderr.fileno()
                        except ValueError:  # file is already closed
                            time.sleep(0.5)
                            continue

                        try:
                            l = os.read(fd, 1024)
                        except OSError:
                            time.sleep(0.5)
                            continue

                        if not l:
                            continue
                        tots = len(l)
                        for lx in la:
                            tots += len(lx)
                        while tots > 1 << 20 and la:
                            tots -= len(la.pop(0))
                        la.append(l)
                    finally:
                        po.lock.release()
        t = Thread(target=tailer)
        t.start()
        cls.errhandler = t

    @classmethod
    def fork(cls):
        """fork wrapper that restarts errhandler thread in child"""
        pid = os.fork()
        if not pid:
            cls.init_errhandler()
        return pid

    def __init__(self, args, *a, **kw):
        """customizations for subprocess.Popen instantiation

        - 'close_fds' is taken to be the default
        - if child's stderr is chosen to be managed,
          register it with the error handler thread
        """
        self.args = args
        if 'close_fds' not in kw:
            kw['close_fds'] = True
        self.lock = threading.Lock()
        self.on_death_row = False
        self.elines = []
        try:
            sup(self, args, *a, **kw)
        except:
            ex = sys.exc_info()[1]
            if not isinstance(ex, OSError):
                raise
            raise GsyncdError("""execution of "%s" failed with %s (%s)""" %
                              (args[0], errno.errorcode[ex.errno],
                               os.strerror(ex.errno)))
        if kw.get('stderr') == subprocess.PIPE:
            assert(getattr(self, 'errhandler', None))
            self.errstore[self] = []

    def errlog(self):
        """make a log about child's failure event"""
        logging.error(lf("command returned error",
                         cmd=" ".join(self.args),
                         error=self.returncode))
        lp = ''

        def logerr(l):
            logging.error(self.args[0] + "> " + l)
        for l in self.elines:
            ls = l.split('\n')
            ls[0] = lp + ls[0]
            lp = ls.pop()
            for ll in ls:
                logerr(ll)
        if lp:
            logerr(lp)

    def errfail(self):
        """fail nicely if child did not terminate with success"""
        self.errlog()
        finalize(exval=1)

    def terminate_geterr(self, fail_on_err=True):
        """kill child, finalize stderr harvesting (unregister
        from errhandler, set up .elines), fail on error if
        asked for
        """
        self.lock.acquire()
        try:
            self.on_death_row = True
        finally:
            self.lock.release()
        elines = self.errstore.pop(self)
        if self.poll() is None:
            self.terminate()
            if self.poll() is None:
                time.sleep(0.1)
                self.kill()
                self.wait()
        while True:
            if not select([self.stderr], [], [], 0.1)[0]:
                break
            b = os.read(self.stderr.fileno(), 1024)
            if b:
                elines.append(b)
            else:
                break
        self.stderr.close()
        self.elines = elines
        if fail_on_err and self.returncode != 0:
            self.errfail()


class Volinfo(object):

    def __init__(self, vol, host='localhost', prelude=[]):
        po = Popen(prelude + ['gluster', '--xml', '--remote-host=' + host,
                              'volume', 'info', vol],
                   stdout=PIPE, stderr=PIPE)
        vix = po.stdout.read()
        po.wait()
        po.terminate_geterr()
        vi = XET.fromstring(vix)
        if vi.find('opRet').text != '0':
            if prelude:
                via = '(via %s) ' % prelude.join(' ')
            else:
                via = ' '
            raise GsyncdError('getting volume info of %s%s '
                              'failed with errorcode %s' %
                              (vol, via, vi.find('opErrno').text))
        self.tree = vi
        self.volume = vol
        self.host = host

    def get(self, elem):
        return self.tree.findall('.//' + elem)

    def is_tier(self):
        return (self.get('typeStr')[0].text == 'Tier')

    def is_hot(self, brickpath):
        logging.debug('brickpath: ' + repr(brickpath))
        return brickpath in self.hot_bricks

    @property
    @memoize
    def bricks(self):
        def bparse(b):
            host, dirp = b.find("name").text.split(':', 2)
            return {'host': host, 'dir': dirp, 'uuid': b.find("hostUuid").text}
        return [bparse(b) for b in self.get('brick')]

    @property
    @memoize
    def uuid(self):
        ids = self.get('id')
        if len(ids) != 1:
            raise GsyncdError("volume info of %s obtained from %s: "
                              "ambiguous uuid" % (self.volume, self.host))
        return ids[0].text

    def replica_count(self, tier, hot):
        if (tier and hot):
            return int(self.get('hotBricks/hotreplicaCount')[0].text)
        elif (tier and not hot):
            return int(self.get('coldBricks/coldreplicaCount')[0].text)
        else:
            return int(self.get('replicaCount')[0].text)

    def disperse_count(self, tier, hot):
        if (tier and hot):
            # Tiering doesn't support disperse volume as hot brick,
            # hence no xml output, so returning 0. In case, if it's
            # supported later, we should change here.
            return 0
        elif (tier and not hot):
            return int(self.get('coldBricks/colddisperseCount')[0].text)
        else:
            return int(self.get('disperseCount')[0].text)

    @property
    @memoize
    def hot_bricks(self):
        return [b.text for b in self.get('hotBricks/brick')]

    def get_hot_bricks_count(self, tier):
        if (tier):
            return int(self.get('hotBricks/hotbrickCount')[0].text)
        else:
            return 0
