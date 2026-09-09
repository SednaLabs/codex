use std::os::fd::AsRawFd;
use std::os::fd::FromRawFd;
use std::os::fd::OwnedFd;

/// Relays output from a bubblewrap process without waiting for orphaned
/// descendants that keep bubblewrap's PID-namespace reaper alive.
pub(crate) struct BwrapOutputRelay {
    stdout_read: OwnedFd,
    stdout_write: OwnedFd,
    stderr_read: OwnedFd,
    stderr_write: OwnedFd,
}

impl BwrapOutputRelay {
    pub(crate) fn new() -> Self {
        let (stdout_read, stdout_write) = create_pipe("stdout");
        let (stderr_read, stderr_write) = create_pipe("stderr");
        Self {
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
        }
    }

    pub(crate) fn redirect_child_output(self) {
        let Self {
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
        } = self;
        drop(stdout_read);
        drop(stderr_read);
        dup2_or_panic(stdout_write.as_raw_fd(), libc::STDOUT_FILENO, "stdout");
        dup2_or_panic(stderr_write.as_raw_fd(), libc::STDERR_FILENO, "stderr");
    }

    pub(crate) fn forward_until_child_exit(self, pid: libc::pid_t) -> libc::c_int {
        let Self {
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
        } = self;
        drop(stdout_write);
        drop(stderr_write);
        set_nonblocking(stdout_read.as_raw_fd(), "stdout");
        set_nonblocking(stderr_read.as_raw_fd(), "stderr");

        loop {
            forward_available(stdout_read.as_raw_fd(), libc::STDOUT_FILENO, "stdout");
            forward_available(stderr_read.as_raw_fd(), libc::STDERR_FILENO, "stderr");
            if let Some(status) = try_wait_for_child(pid) {
                forward_available(stdout_read.as_raw_fd(), libc::STDOUT_FILENO, "stdout");
                forward_available(stderr_read.as_raw_fd(), libc::STDERR_FILENO, "stderr");
                return status;
            }
            poll_for_output(&stdout_read, &stderr_read);
        }
    }

    /// Drain both streams until all writers have closed. This is used by the
    /// detached-custody path, which must keep the foreground command
    /// unblocked without waiting for intentionally orphaned descendants.
    pub(crate) fn forward_until_eof(self) {
        let Self {
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
        } = self;
        drop(stdout_write);
        drop(stderr_write);
        set_nonblocking(stdout_read.as_raw_fd(), "stdout");
        set_nonblocking(stderr_read.as_raw_fd(), "stderr");
        let mut stdout_closed = false;
        let mut stderr_closed = false;
        while !(stdout_closed && stderr_closed) {
            if !stdout_closed {
                stdout_closed =
                    forward_available(stdout_read.as_raw_fd(), libc::STDOUT_FILENO, "stdout");
            }
            if !stderr_closed {
                stderr_closed =
                    forward_available(stderr_read.as_raw_fd(), libc::STDERR_FILENO, "stderr");
            }
            if !(stdout_closed && stderr_closed) {
                poll_for_output(&stdout_read, &stderr_read);
            }
        }
    }
}

fn create_pipe(stream: &str) -> (OwnedFd, OwnedFd) {
    let mut pipe_fds = [0; 2];
    if unsafe { libc::pipe2(pipe_fds.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to create bubblewrap {stream} relay pipe: {err}");
    }

    // Keep relay descriptors out of stdin/stdout/stderr. If the caller had a
    // closed standard stream, pipe2 could otherwise return fd 0, 1, or 2;
    // dup2 followed by OwnedFd drop would then close the stream we just set.
    let stdout_read = reserve_fd_above_stdio(pipe_fds[0], stream);
    let stdout_write = reserve_fd_above_stdio(pipe_fds[1], stream);

    // SAFETY: pipe2 initialized both returned file descriptors and transfers
    // their ownership to this function.
    unsafe {
        (
            OwnedFd::from_raw_fd(stdout_read),
            OwnedFd::from_raw_fd(stdout_write),
        )
    }
}

fn reserve_fd_above_stdio(fd: libc::c_int, stream: &str) -> libc::c_int {
    if fd > libc::STDERR_FILENO {
        return fd;
    }
    let reserved = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, libc::STDERR_FILENO + 1) };
    if reserved < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to reserve bubblewrap {stream} relay fd: {err}");
    }
    if unsafe { libc::close(fd) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to close temporary bubblewrap {stream} relay fd: {err}");
    }
    reserved
}

fn dup2_or_panic(source: libc::c_int, target: libc::c_int, stream: &str) {
    if unsafe { libc::dup2(source, target) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to redirect bubblewrap {stream}: {err}");
    }
}

fn set_nonblocking(fd: libc::c_int, stream: &str) {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to configure bubblewrap {stream} relay pipe: {err}");
    }
}

fn forward_available(source: libc::c_int, destination: libc::c_int, stream: &str) -> bool {
    let mut buffer = [0_u8; 8192];
    loop {
        let bytes_read = unsafe { libc::read(source, buffer.as_mut_ptr().cast(), buffer.len()) };
        if bytes_read > 0 {
            write_all(destination, &buffer[..bytes_read as usize], stream);
            continue;
        }
        if bytes_read == 0 {
            return true;
        }
        let err = std::io::Error::last_os_error();
        match err.kind() {
            std::io::ErrorKind::Interrupted => continue,
            std::io::ErrorKind::WouldBlock => return false,
            _ => panic!("failed to read bubblewrap {stream} output: {err}"),
        }
    }
}

fn write_all(fd: libc::c_int, mut bytes: &[u8], stream: &str) {
    while !bytes.is_empty() {
        let bytes_written = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if bytes_written > 0 {
            bytes = &bytes[bytes_written as usize..];
            continue;
        }
        let err = std::io::Error::last_os_error();
        if err.kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        panic!("failed to forward bubblewrap {stream} output: {err}");
    }
}

fn try_wait_for_child(pid: libc::pid_t) -> Option<libc::c_int> {
    let mut status = 0;
    let result = unsafe { libc::waitpid(pid, &mut status, libc::WNOHANG) };
    if result == pid {
        return Some(status);
    }
    if result == 0 {
        return None;
    }
    let err = std::io::Error::last_os_error();
    if err.kind() == std::io::ErrorKind::Interrupted {
        return None;
    }
    panic!("waitpid failed for bubblewrap child: {err}");
}

fn poll_for_output(stdout_read: &OwnedFd, stderr_read: &OwnedFd) {
    let mut poll_fds = [
        libc::pollfd {
            fd: stdout_read.as_raw_fd(),
            events: libc::POLLIN | libc::POLLHUP,
            revents: 0,
        },
        libc::pollfd {
            fd: stderr_read.as_raw_fd(),
            events: libc::POLLIN | libc::POLLHUP,
            revents: 0,
        },
    ];
    let result = unsafe { libc::poll(poll_fds.as_mut_ptr(), poll_fds.len() as _, 10) };
    if result < 0 {
        let err = std::io::Error::last_os_error();
        if err.kind() != std::io::ErrorKind::Interrupted {
            panic!("failed to poll bubblewrap output relay pipes: {err}");
        }
    }
}
