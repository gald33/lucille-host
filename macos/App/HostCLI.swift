import Foundation

#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// How a child ended, as Foundation's `terminationReason` says, apart from its number.
enum EndReason: Equatable, Sendable {
    /// It exited: the number is its exit status.
    case exit
    /// A signal ended it: the number is the signal's. 20 to 25 are exit codes of `run` and macOS
    /// signal numbers too (spec/CLI.md §13.3), so how a child ended is read before its number.
    case uncaughtSignal
}

/// How a child ended, with its number.
enum ChildEnd: Equatable, Sendable {
    case exited(Int32)
    case signaled(Int32)
}

/// What one run of the host's command line did.
struct CLIResult: Equatable, Sendable {
    /// The status when the program could not be started at all.
    static let notStarted: Int32 = -1
    /// The status when a stopped run did not end, even after SIGKILL, and the app gave up waiting.
    static let notFinished: Int32 = -2

    /// The exit status, or, when a signal ended the run, the signal's number.
    let status: Int32
    let stdout: String
    let stderr: String
    let timedOut: Bool
    /// The user cancelled the run, and the app stopped it.
    var cancelled = false
    /// How the run ended. A signal is a crash, never the exit code of the same number.
    var terminationReason: EndReason = .exit

    /// The signal that ended the run, if one did.
    var signal: Int32? { terminationReason == .uncaughtSignal ? status : nil }

    var succeeded: Bool { status == 0 && terminationReason == .exit && !timedOut && !cancelled }
}

/// Lets the user stop a run before its time limit. `HostCLI.run` looks at it every tenth of a
/// second, and stops the program once it is cancelled.
final class CancelToken: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    func cancel() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }
}

/// Runs the host's command line. It is the only program the app starts: the Python interpreter
/// inside the app (`lucille-host-runtime`, the bundled runtime's renamed interpreter), with `-I`
/// (isolated: no PYTHON* variables, no user site-packages, no current folder on the import path),
/// `-B` (no .pyc writes inside the signed app; the app ships none) and `-X utf8` (UTF-8 text
/// whatever the locale), running the `lucille_host` package on the app's own profile. These are
/// the flags the build's wrapper and checks use (decisions/0002 §6). Arguments go as an argv
/// array, never through a shell. The environment is fixed, not inherited. Every short run has a
/// time limit, and the user can cancel one; the long-lived children, `pair` and `run`, are each a
/// `HostChild`.
struct HostCLI: Sendable {
    /// The profile the app runs every verb on: its own folders, Keychain item, run lock and
    /// version record, apart from a command-line host's `default` (decisions/0002 §9).
    static let appProfile = "mac-app"

    /// The most a short standard input may be: a pairing code (at most 8192 characters, which
    /// `Pairing.looksLikeCode` checks) and its newline. It is written into the pipe before the
    /// child starts, so it must fit the pipe's buffer, which is 16384 bytes on macOS.
    static let maxInputBytes = 8193

    let python: URL
    let home: String
    let profile: String
    let timeout: TimeInterval

    init(python: URL, home: String, profile: String = HostCLI.appProfile, timeout: TimeInterval = 30) {
        self.python = python
        self.home = home
        self.profile = profile
        self.timeout = timeout
    }

    /// The interpreter inside an app bundle.
    static func python(inApp app: URL) -> URL {
        app.appendingPathComponent("Contents/Resources/runtime/bin/lucille-host-runtime")
    }

    /// The home folder the host uses: HOME when it is an absolute path, as the host reads it.
    static func userHome() -> String {
        if let home = ProcessInfo.processInfo.environment["HOME"], home.hasPrefix("/") {
            return home
        }
        return NSHomeDirectory()
    }

    func arguments(for verb: [String]) -> [String] {
        ["-I", "-B", "-X", "utf8", "-m", "lucille_host"] + ["--profile", profile] + verb
    }

    /// The whole environment a run gets. Nothing else from the app's own environment passes.
    /// PATH names an empty folder, so nothing the host or a library it loads might look up there
    /// is found (decisions/0002 §9); the host runs its operations with a PATH of its own.
    func environment() -> [String: String] {
        var env = ["HOME": home, "PATH": "/var/empty", "LANG": "en_US.UTF-8"]
        let own = ProcessInfo.processInfo.environment
        for key in ["USER", "LOGNAME", "TMPDIR"] {
            if let value = own[key] {
                env[key] = value
            }
        }
        return env
    }

    /// How many runs the app stopped waiting for have still not ended. One may hold the host's
    /// policy lock, and then every change to folders and abilities waits for it.
    static var stuckRuns: Int { leftBehind.running }

    private static let leftBehind = LeftBehind()

    /// Run one verb and wait for it, up to the time limit (`timeout`, or this command line's) or
    /// until `cancel` is cancelled. `input`, if given, is the verb's whole standard input: it is
    /// written into the pipe before the verb starts, and the app's end is then closed, so a verb
    /// that exits without reading it cannot break the write. A run that is stopped gets SIGTERM,
    /// then SIGKILL 2 seconds later. If it still has not ended 2 seconds after that (a process
    /// stuck on a stalled network drive cannot end), the app stops waiting for it, counts it in
    /// `stuckRuns` until it ends, and the result has the status `notFinished`.
    func run(
        _ verb: [String], input: Data? = nil, cancel: CancelToken? = nil, timeout: TimeInterval? = nil
    ) -> CLIResult {
        let exited = DispatchSemaphore(value: 0)
        let out = Collected()
        let err = Collected()
        let child: Spawned
        do {
            child = try spawn(
                verb,
                stdin: input.map { Stdin.bytes($0) } ?? .nothing,
                stdout: { out.take($0) },
                stderr: { err.take($0) },
                ended: { _ in exited.signal() }
            )
        } catch {
            return CLIResult(
                status: CLIResult.notStarted,
                stdout: "",
                stderr: "\(HostText.appName) could not start \(python.path): \(error.localizedDescription)",
                timedOut: false
            )
        }
        let process = child.process
        let deadline = Date().addingTimeInterval(timeout ?? self.timeout)
        var timedOut = false
        var cancelled = false
        while true {
            if cancel?.isCancelled == true {
                cancelled = true
                break
            }
            let step = min(0.1, max(0, deadline.timeIntervalSinceNow))
            if exited.wait(timeout: .now() + step) == .success {
                break
            }
            if Date() >= deadline {
                timedOut = true
                break
            }
        }
        if timedOut || cancelled {
            if process.isRunning {
                process.terminate()
            }
            if exited.wait(timeout: .now() + 2) == .timedOut {
                if process.isRunning {
                    kill(process.processIdentifier, SIGKILL)
                }
                if exited.wait(timeout: .now() + 2) == .timedOut {
                    HostCLI.leftBehind.add(process)
                    return CLIResult(
                        status: CLIResult.notFinished,
                        stdout: "",
                        stderr: "",
                        timedOut: timedOut,
                        cancelled: cancelled
                    )
                }
            }
        }
        // What the verb wrote can still be in the pipes when its exit is seen: read both to their
        // end, for a few seconds at most, in case a process it left behind holds one open.
        out.waitForEnd(seconds: 5)
        err.waitForEnd(seconds: 5)
        return CLIResult(
            status: process.terminationStatus,
            stdout: out.text,
            stderr: err.text,
            timedOut: timedOut,
            cancelled: cancelled,
            terminationReason: process.terminationReason == .uncaughtSignal ? .uncaughtSignal : .exit
        )
    }

    /// What a child gets on its standard input.
    enum Stdin {
        /// Nothing: the null device.
        case nothing
        /// These bytes, at most `maxInputBytes`: written into the pipe before the child starts,
        /// with the app's end then closed, so the child reads them and then the pipe's end. The
        /// read end is open while they are written, so the write can meet no broken pipe.
        case bytes(Data)
        /// The lifeline: a pipe whose write end the app keeps, close-on-exec, gives to no other
        /// child, and never writes to (spec/CLI.md §13.1).
        case lifeline
    }

    enum SpawnError: Error, Equatable {
        /// The input is longer than `maxInputBytes`, or did not fit the pipe.
        case inputTooLong
    }

    /// A child that started: its process, and the pipes the app keeps until the child has ended.
    struct Spawned {
        let process: Process
        let pipes: [Pipe]
        /// The write end of the lifeline, for `Stdin.lifeline`.
        let lifeline: FileHandle?
    }

    /// Held around every spawn: making the child's pipes, marking the ends the app keeps
    /// close-on-exec, writing a short input, and starting the child. So a child started on another
    /// thread never inherits an end of a pipe that is not its own, such as the lifeline's write
    /// end, between that pipe being made and its end being marked.
    private static let spawnLock = NSLock()

    /// Start `verb`. `stdout` and `stderr` get each stream's bytes as they arrive, on a thread of
    /// their own that reads with read(2), and then nil at its end; both threads start before the
    /// child does, and so does the handler that calls `ended`, which may be called before this
    /// returns. Throws when the child could not start; the readers have then seen their end.
    func spawn(
        _ verb: [String],
        stdin: Stdin,
        stdout: @escaping @Sendable (Data?) -> Void,
        stderr: @escaping @Sendable (Data?) -> Void,
        ended: @escaping @Sendable (ChildEnd) -> Void
    ) throws -> Spawned {
        HostCLI.spawnLock.lock()
        defer { HostCLI.spawnLock.unlock() }
        let process = Process()
        process.executableURL = python
        process.arguments = arguments(for: verb)
        process.environment = environment()
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        let outPipe = Pipe()
        let errPipe = Pipe()
        var pipes = [outPipe, errPipe]
        var lifeline: FileHandle?
        var written: FileHandle?
        switch stdin {
        case .nothing:
            process.standardInput = FileHandle.nullDevice
        case .bytes(let data):
            guard data.count <= HostCLI.maxInputBytes else {
                throw SpawnError.inputTooLong
            }
            let inPipe = Pipe()
            let end = inPipe.fileHandleForWriting
            HostCLI.closeOnExec(end.fileDescriptor)
            guard HostCLI.writeWithoutWaiting(data, to: end.fileDescriptor) else {
                try? end.close()
                try? inPipe.fileHandleForReading.close()
                throw SpawnError.inputTooLong
            }
            process.standardInput = inPipe
            pipes.append(inPipe)
            written = end
        case .lifeline:
            let inPipe = Pipe()
            HostCLI.closeOnExec(inPipe.fileHandleForWriting.fileDescriptor)
            process.standardInput = inPipe
            pipes.append(inPipe)
            lifeline = inPipe.fileHandleForWriting
        }
        process.standardOutput = outPipe
        process.standardError = errPipe
        HostCLI.closeOnExec(outPipe.fileHandleForReading.fileDescriptor)
        HostCLI.closeOnExec(errPipe.fileHandleForReading.fileDescriptor)
        process.terminationHandler = { finished in
            let end: ChildEnd = finished.terminationReason == .uncaughtSignal
                ? .signaled(finished.terminationStatus)
                : .exited(finished.terminationStatus)
            ended(end)
        }
        PipeDrain.start(outPipe.fileHandleForReading, chunk: stdout)
        PipeDrain.start(errPipe.fileHandleForReading, chunk: stderr)
        do {
            try process.run()
        } catch {
            // Nothing started: close every write end, so that both readers see their pipe's end.
            for pipe in pipes {
                try? pipe.fileHandleForWriting.close()
            }
            throw error
        }
        // The child holds the read end, and the bytes are in the pipe: the app's end goes now.
        try? written?.close()
        return Spawned(process: process, pipes: pipes, lifeline: lifeline)
    }

    private static func closeOnExec(_ fd: Int32) {
        let flags = fcntl(fd, F_GETFD)
        if flags >= 0 {
            _ = fcntl(fd, F_SETFD, flags | FD_CLOEXEC)
        }
    }

    /// Write `data` into a pipe nothing reads yet, with the end made non-blocking, so that bytes
    /// that do not fit fail at once instead of waiting forever. False when they did not all fit.
    private static func writeWithoutWaiting(_ data: Data, to fd: Int32) -> Bool {
        let flags = fcntl(fd, F_GETFL)
        guard flags >= 0, fcntl(fd, F_SETFL, flags | O_NONBLOCK) >= 0 else {
            return false
        }
        return data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Bool in
            guard let base = raw.baseAddress else {
                return true
            }
            var offset = 0
            while offset < raw.count {
                let wrote = write(fd, base + offset, raw.count - offset)
                if wrote > 0 {
                    offset += wrote
                } else if wrote < 0 && errno == EINTR {
                    continue
                } else {
                    return false
                }
            }
            return true
        }
    }
}

/// Reads one pipe to its end with read(2), on a thread that does nothing else, so a child that
/// writes a lot never waits on a full pipe, and a read error is an end, not an exception. The
/// thread holds the file handle until the end, so the descriptor stays open while it reads.
private enum PipeDrain {
    static func start(_ handle: FileHandle, chunk: @escaping @Sendable (Data?) -> Void) {
        let thread = Thread {
            let fd = handle.fileDescriptor
            var buffer = [UInt8](repeating: 0, count: 65_536)
            while true {
                let count = buffer.withUnsafeMutableBytes { (raw: UnsafeMutableRawBufferPointer) -> Int in
                    read(fd, raw.baseAddress, raw.count)
                }
                if count > 0 {
                    chunk(Data(buffer[0..<count]))
                } else if count < 0 && errno == EINTR {
                    continue
                } else {
                    break
                }
            }
            chunk(nil)
            withExtendedLifetime(handle) {}
        }
        thread.start()
    }
}

/// The bytes of one stream of a short run, gathered until its end.
private final class Collected: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    private let ended = DispatchSemaphore(value: 0)

    func take(_ chunk: Data?) {
        guard let chunk else {
            ended.signal()
            return
        }
        lock.lock()
        data.append(chunk)
        lock.unlock()
    }

    func waitForEnd(seconds: Double) {
        _ = ended.wait(timeout: .now() + seconds)
    }

    var text: String {
        lock.lock()
        defer { lock.unlock() }
        return String(decoding: data, as: UTF8.self)
    }
}

/// The runs the app stopped waiting for because they would not end.
private final class LeftBehind: @unchecked Sendable {
    private let lock = NSLock()
    private var processes: [Process] = []

    func add(_ process: Process) {
        lock.lock()
        processes.append(process)
        lock.unlock()
    }

    /// How many of them are still running. Those that have ended are forgotten.
    var running: Int {
        lock.lock()
        defer { lock.unlock() }
        processes.removeAll { !$0.isRunning }
        return processes.count
    }
}

/// How a long-lived child ended, and whether its standard output had reached its end by then.
struct ChildExit: Equatable, Sendable {
    let end: ChildEnd
    /// False when standard output was still open 2 seconds after the exit (a process the child
    /// left behind may hold it): the app decided on what it had read.
    let stdoutComplete: Bool
}

/// The one long-lived child type: `pair --code-stdin --json`, which waits for Lucille, and
/// `run --json-events --exit-on-stdin-eof`, which the app supervises (spec/CLI.md §13). Both
/// output streams are read as they come, on threads of their own, until the child has exited,
/// during a quit too. Standard output is read as JSON lines (`JSONLines`); each object, each line
/// that could not be used and each line of standard error is delivered on `deliverOn`, in order,
/// and `onExit` last, once standard output has ended or 2 seconds have passed. Nothing is
/// delivered after `onExit`. Set the callbacks before `start()`. The child keeps itself alive
/// until `onExit` has been delivered.
final class HostChild: @unchecked Sendable {
    /// What the child gets on its standard input.
    enum Input: Sendable {
        /// A pairing code, written into the pipe before the child starts, with a newline. It goes
        /// into neither argv nor any log.
        case code(String)
        /// The lifeline, which the app holds and never writes to; `closeLifeline()` closes it.
        case lifeline
    }

    enum StartError: Error, Equatable {
        case alreadyStarted
        case inputTooLong
    }

    /// How many lines of standard error are kept, for a note that shows them.
    static let stderrLinesKept = 40
    /// The longest line of standard error kept; the rest of a longer one is dropped.
    static let stderrLineBytes = 4096
    /// How long the app waits after the exit for standard output to end (spec/CLI.md §13.3).
    static let exitWait: TimeInterval = 2

    let verb: [String]
    var onObject: ((HostObject) -> Void)?
    var onUnusableLine: ((String) -> Void)?
    var onStderrLine: ((String) -> Void)?
    var onExit: ((ChildExit) -> Void)?

    private let cli: HostCLI
    private let queue: DispatchQueue
    private let lock = NSLock()
    private var input: Input?
    private var started = false
    private var finished = false
    private var lifelineClosed = false
    private var spawned: HostCLI.Spawned?
    private var tail: [String] = []
    private let stdoutEnded = DispatchSemaphore(value: 0)
    private let stderrEnded = DispatchSemaphore(value: 0)
    // Each touched only by the thread that reads its stream.
    private var lines = JSONLines()
    private var errLine: [UInt8] = []

    init(cli: HostCLI, verb: [String], input: Input, deliverOn queue: DispatchQueue) {
        self.cli = cli
        self.verb = verb
        self.input = input
        self.queue = queue
    }

    /// Start the child. Throws, with nothing started and nothing delivered, when it could not.
    func start() throws {
        lock.lock()
        if started {
            lock.unlock()
            throw StartError.alreadyStarted
        }
        started = true
        let given = input
        input = nil
        lock.unlock()
        let stdin: HostCLI.Stdin
        switch given {
        case .code(let code)?:
            let bytes = Data((code + "\n").utf8)
            guard bytes.count <= HostCLI.maxInputBytes else {
                throw StartError.inputTooLong
            }
            stdin = .bytes(bytes)
        case .lifeline?, nil:
            stdin = .lifeline
        }
        let made = try cli.spawn(
            verb,
            stdin: stdin,
            stdout: { [self] chunk in self.readStdout(chunk) },
            stderr: { [self] chunk in self.readStderr(chunk) },
            ended: { [self] end in self.childEnded(end) }
        )
        lock.lock()
        spawned = made
        let over = finished
        lock.unlock()
        if over {
            made.process.terminationHandler = nil
        }
    }

    /// The child's process id; 0 before it starts.
    var pid: Int32 {
        lock.lock()
        defer { lock.unlock() }
        return spawned?.process.processIdentifier ?? 0
    }

    var isRunning: Bool {
        lock.lock()
        let process = spawned?.process
        lock.unlock()
        return process?.isRunning ?? false
    }

    /// The last lines of standard error, oldest first.
    var stderrTail: [String] {
        lock.lock()
        defer { lock.unlock() }
        return tail
    }

    /// SIGINT: `pair` stops waiting with `interrupted`, and keeps the pair (spec/CLI.md §4.3).
    func interrupt() {
        signalChild(SIGINT)
    }

    /// SIGTERM.
    func terminate() {
        signalChild(SIGTERM)
    }

    /// SIGKILL, the last resort.
    func killNow() {
        signalChild(SIGKILL)
    }

    /// Close the app's end of the lifeline: `run` drains, as on its first SIGTERM, and exits
    /// (spec/CLI.md §13.4). Nothing was ever written to it.
    func closeLifeline() {
        lock.lock()
        let end = lifelineClosed ? nil : spawned?.lifeline
        lifelineClosed = true
        lock.unlock()
        try? end?.close()
    }

    /// A signal to the child, only while it runs, so a late call never reaches a process that has
    /// since taken its pid.
    private func signalChild(_ number: Int32) {
        lock.lock()
        let process = spawned?.process
        lock.unlock()
        guard let process, process.isRunning else {
            return
        }
        kill(process.processIdentifier, number)
    }

    private func readStdout(_ chunk: Data?) {
        let items: [JSONLines.Item]
        if let chunk {
            items = lines.feed(chunk)
        } else {
            items = lines.finish()
        }
        if !items.isEmpty {
            queue.async { self.deliver(items) }
        }
        if chunk == nil {
            stdoutEnded.signal()
        }
    }

    private func readStderr(_ chunk: Data?) {
        var done: [String] = []
        if let chunk {
            for byte in chunk {
                if byte == 0x0A {
                    done.append(String(decoding: errLine, as: UTF8.self))
                    errLine.removeAll(keepingCapacity: true)
                } else if errLine.count < HostChild.stderrLineBytes {
                    errLine.append(byte)
                }
            }
        } else if !errLine.isEmpty {
            done.append(String(decoding: errLine, as: UTF8.self))
            errLine.removeAll()
        }
        if !done.isEmpty {
            lock.lock()
            tail.append(contentsOf: done)
            if tail.count > HostChild.stderrLinesKept {
                tail.removeFirst(tail.count - HostChild.stderrLinesKept)
            }
            lock.unlock()
            let batch = done
            queue.async { self.deliverStderr(batch) }
        }
        if chunk == nil {
            stderrEnded.signal()
        }
    }

    private func deliver(_ items: [JSONLines.Item]) {
        lock.lock()
        let over = finished
        lock.unlock()
        guard !over else {
            return
        }
        for item in items {
            switch item {
            case .object(let object):
                onObject?(object)
            case .unusable(let note):
                onUnusableLine?(note)
            }
        }
    }

    private func deliverStderr(_ batch: [String]) {
        lock.lock()
        let over = finished
        lock.unlock()
        guard !over else {
            return
        }
        for line in batch {
            onStderrLine?(line)
        }
    }

    /// The process has ended. Wait, off the delivery queue, for standard output to end (2 s at
    /// most), then deliver the exit after everything read before it.
    private func childEnded(_ end: ChildEnd) {
        DispatchQueue.global(qos: .userInitiated).async {
            let deadline = DispatchTime.now() + HostChild.exitWait
            let complete = self.stdoutEnded.wait(timeout: deadline) == .success
            _ = self.stderrEnded.wait(timeout: deadline)
            self.queue.async { self.exitNow(ChildExit(end: end, stdoutComplete: complete)) }
        }
    }

    private func exitNow(_ exit: ChildExit) {
        lock.lock()
        finished = true
        let process = spawned?.process
        lock.unlock()
        // The handler held this child alive until now.
        process?.terminationHandler = nil
        onExit?(exit)
    }
}
